import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models import Order, OrderItem, OrderStatus
from services.reporting import EAT, sanitize_csv_cell
from tests.test_orders import register_merchant_headers


async def test_fulfill_order_lifecycle(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str]] = []

    async def fake_send_wa(to_phone: str, body: str) -> bool:
        sent.append((to_phone, body))
        return True

    monkeypatch.setattr("routers.order.send_whatsapp_text", fake_send_wa)

    m1_id, h1 = await register_merchant_headers(client, 1)
    _, h2 = await register_merchant_headers(client, 2)

    paid = Order(
        merchant_id=m1_id,
        customer_phone="254700111222",
        total_amount=Decimal("640.00"),
        status=OrderStatus.PAID,
        mpesa_receipt_number="REC001",
    )
    pending = Order(
        merchant_id=m1_id,
        customer_phone="254700111223",
        total_amount=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )
    failed = Order(
        merchant_id=m1_id,
        customer_phone="254700111224",
        total_amount=Decimal("100.00"),
        status=OrderStatus.FAILED,
    )
    cancelled = Order(
        merchant_id=m1_id,
        customer_phone="254700111225",
        total_amount=Decimal("100.00"),
        status=OrderStatus.CANCELLED,
    )
    db.add_all([paid, pending, failed, cancelled])
    await db.commit()

    # Ownership, validation, and status guards
    assert (await client.post("/orders/0/fulfill", headers=h1)).status_code == 422
    assert (await client.post("/orders/99999/fulfill", headers=h1)).status_code == 404
    assert (await client.post(f"/orders/{paid.id}/fulfill", headers=h2)).status_code == 404
    assert (await client.post(f"/orders/{pending.id}/fulfill", headers=h1)).status_code == 409
    assert (await client.post(f"/orders/{failed.id}/fulfill", headers=h1)).status_code == 409
    assert (await client.post(f"/orders/{cancelled.id}/fulfill", headers=h1)).status_code == 409

    resp = await client.post(f"/orders/{paid.id}/fulfill", headers=h1)
    assert resp.status_code == 200
    assert resp.json()["status"] == "fulfilled"
    assert len(sent) == 1
    assert sent[0][0] == "254700111222"
    assert f"Order #{paid.id}" in sent[0][1]

    # Fulfilling again returns 409
    assert (await client.post(f"/orders/{paid.id}/fulfill", headers=h1)).status_code == 409


async def test_order_summary_and_whatsapp_digest(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    digests: list[tuple[str, str]] = []

    async def fake_send_wa(to_phone: str, body: str) -> bool:
        digests.append((to_phone, body))
        return True

    monkeypatch.setattr("routers.order.send_whatsapp_text", fake_send_wa)

    m1_id, h1 = await register_merchant_headers(client, 1)
    m2_id, h2 = await register_merchant_headers(client, 2)

    # Empty merchant summary & notify (covers empty top_products branch)
    empty_resp = await client.post("/orders/summary/notify", headers=h1)
    assert empty_resp.status_code == 200
    assert empty_resp.json()["total_orders"] == 0
    assert Decimal(empty_resp.json()["gross_revenue"]) == Decimal("0.00")
    assert empty_resp.json()["top_products"] == []
    assert len(digests) == 1
    assert digests[0][0] == "254712345671"
    assert "Top Products:" not in digests[0][1]

    # Seed orders across all statuses and dates for m1, plus one paid order for m2
    orders = [
        Order(
            merchant_id=m1_id,
            customer_phone="254700000001",
            total_amount=Decimal("640.00"),
            status=OrderStatus.PAID,
            mpesa_receipt_number="R1",
            items=[
                OrderItem(
                    sku="OIL-1L", product_name="Oil 1L", quantity=2, unit_price=Decimal("320.00")
                )
            ],
        ),
        Order(
            merchant_id=m1_id,
            customer_phone="254700000002",
            total_amount=Decimal("880.00"),
            status=OrderStatus.FULFILLED,
            mpesa_receipt_number="R2",
            items=[
                OrderItem(
                    sku="OIL-1L", product_name="Oil 1L", quantity=1, unit_price=Decimal("320.00")
                ),
                OrderItem(
                    sku="SUGAR-2KG",
                    product_name="Sugar 2KG",
                    quantity=2,
                    unit_price=Decimal("280.00"),
                ),
            ],
        ),
        Order(
            merchant_id=m1_id,
            customer_phone="254700000003",
            total_amount=Decimal("500.00"),
            status=OrderStatus.PENDING,
        ),
        Order(
            merchant_id=m1_id,
            customer_phone="254700000004",
            total_amount=Decimal("100.00"),
            status=OrderStatus.FAILED,
        ),
        Order(
            merchant_id=m1_id,
            customer_phone="254700000005",
            total_amount=Decimal("150.00"),
            status=OrderStatus.CANCELLED,
        ),
        Order(
            merchant_id=m2_id,
            customer_phone="254700000099",
            total_amount=Decimal("9999.00"),
            status=OrderStatus.PAID,
            mpesa_receipt_number="R_M2",
        ),
    ]
    db.add_all(orders)
    await db.commit()

    # Backdate the PAID order (orders[0]) to 5 days ago to test date filtering
    five_days_ago = datetime.now(UTC) - timedelta(days=5)
    await db.execute(
        text("UPDATE orders SET created_at = :ts WHERE id = :id"),
        {"ts": five_days_ago, "id": orders[0].id},
    )
    await db.commit()

    summary = (await client.get("/orders/summary", headers=h1)).json()
    assert summary["total_orders"] == 5
    assert summary["paid_orders"] == 1
    assert summary["fulfilled_orders"] == 1
    assert summary["pending_orders"] == 1
    assert summary["failed_orders"] == 1
    assert summary["cancelled_orders"] == 1
    assert Decimal(summary["gross_revenue"]) == Decimal("1520.00")
    assert Decimal(summary["pending_value"]) == Decimal("500.00")
    assert [tp["sku"] for tp in summary["top_products"]] == ["OIL-1L", "SUGAR-2KG"]
    assert summary["top_products"][0]["quantity_sold"] == 3
    assert Decimal(summary["top_products"][0]["revenue"]) == Decimal("960.00")

    # Notify with populated top_products
    notify_resp = await client.post("/orders/summary/notify", headers=h1)
    assert notify_resp.status_code == 200
    assert "Top Products:" in digests[1][1]
    assert "OIL-1L: 3 units" in digests[1][1]

    # Date filtering (EAT): today only excludes the 5-day-old PAID order
    today_eat = datetime.now(EAT).date()
    today_str = today_eat.isoformat()
    yesterday_str = (today_eat - timedelta(days=1)).isoformat()

    today_only = (
        await client.get(
            "/orders/summary", headers=h1, params={"start_date": today_str, "end_date": today_str}
        )
    ).json()
    assert today_only["total_orders"] == 4
    assert today_only["paid_orders"] == 0
    assert Decimal(today_only["gross_revenue"]) == Decimal("880.00")

    start_only = (
        await client.get("/orders/summary", headers=h1, params={"start_date": today_str})
    ).json()
    assert start_only["total_orders"] == 4
    assert start_only["paid_orders"] == 0

    end_only = (
        await client.get("/orders/summary", headers=h1, params={"end_date": yesterday_str})
    ).json()
    assert end_only["total_orders"] == 1
    assert end_only["paid_orders"] == 1
    assert Decimal(end_only["gross_revenue"]) == Decimal("640.00")

    # Invalid date range -> 422 on both endpoints
    bad_params = {"start_date": "2026-11-20", "end_date": "2026-11-10"}
    assert (await client.get("/orders/summary", headers=h1, params=bad_params)).status_code == 422
    assert (
        await client.post("/orders/summary/notify", headers=h1, params=bad_params)
    ).status_code == 422
    assert (await client.get("/orders/summary", headers=h2)).json()["total_orders"] == 1


async def test_order_summary_eat_bounds_and_top_products_cap(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    digests: list[tuple[str, str]] = []

    async def fake_send_wa(to_phone: str, body: str) -> bool:
        digests.append((to_phone, body))
        return True

    monkeypatch.setattr("routers.order.send_whatsapp_text", fake_send_wa)

    m_id, headers = await register_merchant_headers(client, 1)

    # Create 12 SKUs: SKU-01 leads revenue (200.00), SKU-02..SKU-12 tie at 100.00 each
    # to verify top-10 SQL cap, alphabetical tie-break, and 5-item WhatsApp digest cap.
    items = [
        OrderItem(
            sku="SKU-01", product_name="Product 01", quantity=2, unit_price=Decimal("100.00")
        ),
        *[
            OrderItem(
                sku=f"SKU-{i:02d}",
                product_name=f"Product {i:02d}",
                quantity=1,
                unit_price=Decimal("100.00"),
            )
            for i in range(2, 13)
        ],
    ]
    # 2025-05-10 22:00 UTC == 2025-05-11 01:00 EAT (belongs to 2025-05-11 in Kenya, not May 10)
    eat_early_morning = Order(
        merchant_id=m_id,
        customer_phone="254700100100",
        total_amount=Decimal("1300.00"),
        status=OrderStatus.PAID,
        mpesa_receipt_number="REC_EAT_1",
        items=items,
    )
    # 2025-05-11 21:00 UTC == 2025-05-12 00:00 EAT (belongs to 2025-05-12 EAT)
    eat_next_midnight = Order(
        merchant_id=m_id,
        customer_phone="254700100101",
        total_amount=Decimal("50.00"),
        status=OrderStatus.FULFILLED,
        mpesa_receipt_number="REC_EAT_2",
    )
    db.add_all([eat_early_morning, eat_next_midnight])
    await db.commit()

    await db.execute(
        text("UPDATE orders SET created_at = :ts WHERE id = :id"),
        {"ts": datetime(2025, 5, 10, 22, 0, tzinfo=UTC), "id": eat_early_morning.id},
    )
    await db.execute(
        text("UPDATE orders SET created_at = :ts WHERE id = :id"),
        {"ts": datetime(2025, 5, 11, 21, 0, tzinfo=UTC), "id": eat_next_midnight.id},
    )
    await db.commit()

    may_10 = (
        await client.get(
            "/orders/summary",
            headers=headers,
            params={"start_date": "2025-05-10", "end_date": "2025-05-10"},
        )
    ).json()
    assert may_10["total_orders"] == 0

    may_11 = (
        await client.get(
            "/orders/summary",
            headers=headers,
            params={"start_date": "2025-05-11", "end_date": "2025-05-11"},
        )
    ).json()
    assert may_11["total_orders"] == 1
    assert Decimal(may_11["gross_revenue"]) == Decimal("1300.00")
    assert [tp["sku"] for tp in may_11["top_products"]] == [f"SKU-{i:02d}" for i in range(1, 11)]

    await client.post("/orders/summary/notify", headers=headers)
    assert len(digests) == 1
    assert "SKU-05" in digests[0][1]
    assert "SKU-06" not in digests[0][1]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("254712345678", "254712345678"),
        ("=SUM(A1:A2)", "'=SUM(A1:A2)"),
        ("+254700", "'+254700"),
        ("-100", "'-100"),
        ("@cmd", "'@cmd"),
        ("\tcell", "'\tcell"),
        ("\rcell", "'\rcell"),
        (" =SUM(A1:A2)", "' =SUM(A1:A2)"),
        ("   +cmd", "'   +cmd"),
        (" @test", "' @test"),
        (" \t=SUM(1)", "' \t=SUM(1)"),
    ],
)
def test_sanitize_csv_cell(raw: object, expected: str) -> None:
    assert sanitize_csv_cell(raw) == expected


async def test_export_orders_csv(client: AsyncClient, db: AsyncSession) -> None:
    m1_id, h1 = await register_merchant_headers(client, 1)
    m2_id, _ = await register_merchant_headers(client, 2)

    o1 = Order(
        merchant_id=m1_id,
        customer_phone="254700000001",
        total_amount=Decimal("320.00"),
        status=OrderStatus.PAID,
        mpesa_receipt_number="REC1",
        checkout_request_id='=HYPERLINK("http://evil")',
        items=[
            OrderItem(sku="OIL-1L", product_name="Oil", quantity=1, unit_price=Decimal("320.00"))
        ],
    )
    o2 = Order(
        merchant_id=m1_id,
        customer_phone="254700000002",
        total_amount=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )
    o_other = Order(
        merchant_id=m2_id,
        customer_phone="254700000099",
        total_amount=Decimal("999.00"),
        status=OrderStatus.PAID,
        mpesa_receipt_number="REC_OTHER",
    )
    db.add_all([o1, o2, o_other])
    await db.commit()

    # Backdate o1 by 3 days to exercise start_date/end_date filtering in CSV export
    await db.execute(
        text("UPDATE orders SET created_at = :ts WHERE id = :id"),
        {"ts": datetime.now(UTC) - timedelta(days=3), "id": o1.id},
    )
    await db.commit()

    resp = await client.get("/orders/export.csv", headers=h1)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == 'attachment; filename="orders.csv"'
    assert resp.headers["x-content-type-options"] == "nosniff"

    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == [
        "order_id",
        "created_at",
        "customer_phone",
        "status",
        "total_amount",
        "mpesa_receipt_number",
        "checkout_request_id",
        "items_summary",
    ]
    assert len(rows) == 3  # header + 2 orders for m1 (excludes m2)
    paid_row = next(r for r in rows[1:] if r[3] == "paid")
    pending_row = next(r for r in rows[1:] if r[3] == "pending")
    assert paid_row[5] == "REC1"
    assert paid_row[6].startswith("'=")
    assert paid_row[7] == "OIL-1Lx1"
    assert pending_row[7] == ""

    # Status filter, date range filtering, limit slicing, and query validation
    paid_csv = await client.get("/orders/export.csv", headers=h1, params={"status": "paid"})
    assert len(list(csv.reader(io.StringIO(paid_csv.text)))) == 2

    today_str = datetime.now(EAT).date().isoformat()
    today_csv = await client.get(
        "/orders/export.csv", headers=h1, params={"start_date": today_str, "end_date": today_str}
    )
    today_rows = list(csv.reader(io.StringIO(today_csv.text)))
    assert len(today_rows) == 2
    assert today_rows[1][0] == str(o2.id)

    limited_csv = await client.get("/orders/export.csv", headers=h1, params={"limit": 1})
    limited_rows = list(csv.reader(io.StringIO(limited_csv.text)))
    assert len(limited_rows) == 2
    assert limited_rows[1][0] == str(o2.id)  # newest order first (order_by Order.id.desc())

    assert (
        await client.get(
            "/orders/export.csv",
            headers=h1,
            params={"start_date": "2026-11-20", "end_date": "2026-11-10"},
        )
    ).status_code == 422
    assert (
        await client.get("/orders/export.csv", headers=h1, params={"limit": 0})
    ).status_code == 422
    assert (
        await client.get("/orders/export.csv", headers=h1, params={"limit": 5001})
    ).status_code == 422
