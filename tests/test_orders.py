from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models import Order, OrderItem, OrderStatus
from tests.test_whatsapp import build_text_webhook, post_webhook


async def register_merchant_headers(
    client: AsyncClient, idx: int = 0
) -> tuple[int, dict[str, str]]:
    resp = await client.post(
        "/merchants/",
        json={
            "name": f"Shop {idx}",
            "email": f"shop{idx}@example.com",
            "phone_number": f"071234567{idx}",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    return body["id"], {"X-API-Key": body["api_key"]}


async def test_orders_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/orders/")).status_code == 401
    assert (await client.get("/orders/1")).status_code == 401
    assert (await client.post("/orders/1/cancel")).status_code == 401
    assert (await client.post("/orders/1/stk-push")).status_code == 401
    assert (await client.post("/orders/expire-stale")).status_code == 401
    assert (await client.get("/orders/summary")).status_code == 401
    assert (await client.post("/orders/summary/notify")).status_code == 401
    assert (await client.get("/orders/export.csv")).status_code == 401
    assert (await client.post("/orders/1/fulfill")).status_code == 401


async def test_list_get_and_cancel_orders(client: AsyncClient, db: AsyncSession) -> None:
    m1_id, h1 = await register_merchant_headers(client, 1)
    _, h2 = await register_merchant_headers(client, 2)

    # Create two products for m1
    p1_resp = await client.post(
        "/products/",
        headers=h1,
        json={"sku": "OIL-1L", "name": "Oil 1L", "unit_price": "320.00", "stock_qty": 10},
    )
    p2_resp = await client.post(
        "/products/",
        headers=h1,
        json={"sku": "SUGAR-2KG", "name": "Sugar 2KG", "unit_price": "280.00", "stock_qty": 5},
    )
    assert p1_resp.status_code == 201
    assert p2_resp.status_code == 201
    pid1 = p1_resp.json()["id"]
    pid2 = p2_resp.json()["id"]

    # Place a multi-item order via WhatsApp directed at m1's phone number
    await post_webhook(
        client, build_text_webhook("ADD OIL-1L 3", msg_id="o1a", display_phone="254712345671")
    )
    await post_webhook(
        client, build_text_webhook("ADD SUGAR-2KG 2", msg_id="o1b", display_phone="254712345671")
    )
    chk = await post_webhook(
        client, build_text_webhook("CHECKOUT", msg_id="o2", display_phone="254712345671")
    )
    order_id = chk["replies"][0]["order_id"]
    assert order_id is not None

    # List orders with status filter, tenant isolation, and pagination bounds
    orders = (await client.get("/orders/", headers=h1)).json()
    assert len(orders) == 1
    assert orders[0]["id"] == order_id
    assert Decimal(orders[0]["total_amount"]) == Decimal("1520.00")
    assert {item["sku"] for item in orders[0]["items"]} == {"OIL-1L", "SUGAR-2KG"}
    assert (await client.get("/orders/", headers=h2)).json() == []

    pending_only = (await client.get("/orders/", headers=h1, params={"status": "pending"})).json()
    assert len(pending_only) == 1
    paid_only = (await client.get("/orders/", headers=h1, params={"status": "paid"})).json()
    assert paid_only == []

    assert (
        await client.get("/orders/", headers=h1, params={"status": "invalid_status"})
    ).status_code == 422
    assert (await client.get("/orders/", headers=h1, params={"limit": 0})).status_code == 422
    assert (await client.get("/orders/", headers=h1, params={"limit": 101})).status_code == 422
    assert (await client.get("/orders/", headers=h1, params={"offset": -1})).status_code == 422
    assert (await client.get("/orders/0", headers=h1)).status_code == 422
    assert (await client.post("/orders/0/cancel", headers=h1)).status_code == 422

    # Get single order; another merchant gets 404
    own_order = await client.get(f"/orders/{order_id}", headers=h1)
    assert own_order.status_code == 200
    assert (await client.get(f"/orders/{order_id}", headers=h2)).status_code == 404
    assert (await client.post(f"/orders/{order_id}/cancel", headers=h2)).status_code == 404

    # Cancel restores reserved stock on BOTH products
    cancelled = await client.post(f"/orders/{order_id}/cancel", headers=h1)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    prod1 = (await client.get(f"/products/{pid1}", headers=h1)).json()
    prod2 = (await client.get(f"/products/{pid2}", headers=h1)).json()
    assert prod1["reserved_qty"] == 0 and prod1["available_qty"] == 10
    assert prod2["reserved_qty"] == 0 and prod2["available_qty"] == 5

    # Cancelling again returns 409; cancelling a PAID order also returns 409
    again = await client.post(f"/orders/{order_id}/cancel", headers=h1)
    assert again.status_code == 409

    paid_order = Order(
        merchant_id=m1_id,
        customer_phone="254700000001",
        total_amount=Decimal("200.00"),
        status=OrderStatus.PAID,
    )
    db.add(paid_order)
    await db.commit()
    assert (await client.post(f"/orders/{paid_order.id}/cancel", headers=h1)).status_code == 409

    # Pagination slicing (newest first: paid_order then order_id)
    page_1 = (await client.get("/orders/", headers=h1, params={"limit": 1, "offset": 0})).json()
    page_2 = (await client.get("/orders/", headers=h1, params={"limit": 1, "offset": 1})).json()
    assert [o["id"] for o in page_1] == [paid_order.id]
    assert [o["id"] for o in page_2] == [order_id]

    # Cancelling an order whose product was deleted still succeeds cleanly
    o2 = Order(
        merchant_id=m1_id,
        customer_phone="254700000001",
        total_amount=Decimal("100.00"),
        status=OrderStatus.PENDING,
        items=[
            OrderItem(
                product_id=None,
                sku="OLD",
                product_name="Old Item",
                quantity=1,
                unit_price=Decimal("100.00"),
            )
        ],
    )
    db.add(o2)
    await db.commit()
    cancel_deleted = await client.post(f"/orders/{o2.id}/cancel", headers=h1)
    assert cancel_deleted.status_code == 200
    assert cancel_deleted.json()["status"] == "cancelled"
    assert (await db.scalar(text("SELECT count(*) FROM orders"))) == 3
