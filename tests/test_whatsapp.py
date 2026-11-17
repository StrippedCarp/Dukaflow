import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models import Merchant, Order, Product
from services.whatsapp import send_whatsapp_text, verify_whatsapp_signature


def sign_payload(raw_body: bytes, secret: str | None = None) -> dict[str, str]:
    key = (secret if secret is not None else settings.whatsapp_app_secret).encode("utf-8")
    digest = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def build_text_webhook(
    text_body: str,
    *,
    msg_id: str = "wamid.1",
    customer_phone: str = "254700111222",
    display_phone: str = "254712345678",
) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"display_phone_number": display_phone},
                            "messages": [
                                {
                                    "id": msg_id,
                                    "from": customer_phone,
                                    "type": "text",
                                    "text": {"body": text_body},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    return json.dumps(payload).encode("utf-8")


def build_interactive_webhook(
    reply_id: str,
    *,
    via_list: bool = False,
    msg_id: str = "wamid.interactive.1",
    customer_phone: str = "254700111222",
    display_phone: str = "254712345678",
) -> bytes:
    interactive: dict[str, Any] = {"type": "list_reply" if via_list else "button_reply"}
    key = "list_reply" if via_list else "button_reply"
    interactive[key] = {"id": reply_id, "title": "Choice"}
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"display_phone_number": display_phone},
                            "messages": [
                                {
                                    "id": msg_id,
                                    "from": customer_phone,
                                    "type": "interactive",
                                    "interactive": interactive,
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    return json.dumps(payload).encode("utf-8")


async def seed_wholesaler(
    db: AsyncSession, *, idx: int = 1, phone: str = "254712345678", with_products: bool = True
) -> tuple[Merchant, list[Product]]:
    merchant = Merchant(
        name=f"Wholesaler {idx}",
        email=f"w{idx}@example.com",
        phone_number=phone,
    )
    db.add(merchant)
    await db.flush()
    products: list[Product] = []
    if with_products:
        p1 = Product(
            merchant_id=merchant.id,
            sku="OIL-1L",
            name="Cooking Oil 1L",
            unit_price=Decimal("320.00"),
            stock_qty=10,
        )
        p2 = Product(
            merchant_id=merchant.id,
            sku="SUGAR-2KG",
            name="Sugar 2KG",
            unit_price=Decimal("280.00"),
            stock_qty=5,
        )
        inactive = Product(
            merchant_id=merchant.id,
            sku="OLD-ITEM",
            name="Discontinued",
            unit_price=Decimal("50.00"),
            stock_qty=5,
            is_active=False,
        )
        db.add_all([p1, p2, inactive])
        products = [p1, p2, inactive]
    await db.commit()
    return merchant, products


async def post_webhook(client: AsyncClient, body: bytes) -> dict[str, Any]:
    resp = await client.post("/webhooks/whatsapp", content=body, headers=sign_payload(body))
    assert resp.status_code == 200
    return dict(resp.json())


async def test_webhook_signature_verification(client: AsyncClient) -> None:
    body = build_text_webhook("CATALOG")

    # Missing header -> 403
    assert (await client.post("/webhooks/whatsapp", content=body)).status_code == 403
    # Wrong algorithm / malformed header -> 403
    assert (
        await client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "md5=abc"}
        )
    ).status_code == 403
    # Wrong HMAC -> 403
    assert (
        await client.post(
            "/webhooks/whatsapp", content=body, headers=sign_payload(body, secret="wrong")
        )
    ).status_code == 403
    # Empty app secret (fail-closed)
    assert verify_whatsapp_signature(body, "sha256=abc", "") is False


async def test_webhook_rejects_malformed_signed_payload(client: AsyncClient) -> None:
    bad_body = b"{not-json"
    resp = await client.post("/webhooks/whatsapp", content=bad_body, headers=sign_payload(bad_body))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Invalid webhook payload"


async def test_webhook_rejects_oversized_payload(client: AsyncClient) -> None:
    huge = b"x" * 262_145
    resp = await client.post("/webhooks/whatsapp", content=huge, headers=sign_payload(huge))
    assert resp.status_code == 413


async def test_webhook_ignores_non_message_and_invalid_sender_events(client: AsyncClient) -> None:
    status_body = json.dumps({"entry": [{"changes": [{"value": {"messages": []}}]}]}).encode()
    assert (await post_webhook(client, status_body))["replies"] == []

    bad_sender = build_text_webhook("CATALOG", customer_phone="12345", msg_id="wamid.bad")
    assert (await post_webhook(client, bad_sender))["replies"] == []

    media_payload = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"id": "wamid.img", "from": "254700111222", "type": "image"}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    assert (await post_webhook(client, media_payload))["replies"] == []


async def test_full_whatsapp_ordering_and_deduplication(
    client: AsyncClient, db: AsyncSession
) -> None:
    merchant, [p1, _, _] = await seed_wholesaler(db)

    # 1. CATALOG lists active in-stock items (excludes inactive OLD-ITEM)
    cat_resp = await post_webhook(client, build_text_webhook("CATALOG", msg_id="w1"))
    cat_text = cat_resp["replies"][0]["text"]
    assert "OIL-1L" in cat_text
    assert "SUGAR-2KG" in cat_text
    assert "OLD-ITEM" not in cat_text

    # 2. ADD OIL-1L 2 -> adds to Redis cart
    add_resp = await post_webhook(client, build_text_webhook("add oil-1l 2", msg_id="w2"))
    assert "Added 2 x OIL-1L" in add_resp["replies"][0]["text"]

    # 3. Duplicate wamid w2 is ignored (does not double-add)
    dup_resp = await post_webhook(client, build_text_webhook("add oil-1l 2", msg_id="w2"))
    assert dup_resp["replies"] == []

    # 4. Exceeding available stock is rejected
    over_resp = await post_webhook(client, build_text_webhook("ADD OIL-1L 9", msg_id="w3"))
    assert "Only 10 units of OIL-1L available" in over_resp["replies"][0]["text"]

    # 5. CART shows line total (2 x 320.00 = 640.00)
    cart_resp = await post_webhook(client, build_text_webhook("CART", msg_id="w4"))
    assert "KES 640.00" in cart_resp["replies"][0]["text"]

    # 6. CHECKOUT reserves stock and creates PENDING Order
    chk_resp = await post_webhook(client, build_text_webhook("CHECKOUT", msg_id="w5"))
    reply = chk_resp["replies"][0]
    assert reply["order_id"] is not None
    assert "KES 640.00" in reply["text"]

    await db.refresh(p1)
    assert p1.reserved_qty == 2
    assert p1.available_qty == 8

    order = await db.get(Order, reply["order_id"])
    assert order is not None
    assert order.merchant_id == merchant.id
    assert order.total_amount == Decimal("640.00")

    # 7. Cart is cleared after checkout
    empty_cart = await post_webhook(client, build_text_webhook("CART", msg_id="w6"))
    assert "cart is empty" in empty_cart["replies"][0]["text"].lower()


async def test_interactive_replies_and_cart_mutations(
    client: AsyncClient, db: AsyncSession
) -> None:
    await seed_wholesaler(db)

    r1 = await post_webhook(client, build_interactive_webhook("cmd:catalog", msg_id="i1"))
    assert "OIL-1L" in r1["replies"][0]["text"]

    r2 = await post_webhook(
        client, build_interactive_webhook("add:OIL-1L:3", via_list=True, msg_id="i2")
    )
    assert "Added 3 x OIL-1L" in r2["replies"][0]["text"]

    r3 = await post_webhook(client, build_interactive_webhook("add:SUGAR-2KG", msg_id="i3"))
    assert "Added 1 x SUGAR-2KG" in r3["replies"][0]["text"]

    r4 = await post_webhook(client, build_interactive_webhook("cmd:cart", msg_id="i4"))
    assert "KES 1240.00" in r4["replies"][0]["text"]

    r5 = await post_webhook(client, build_interactive_webhook("remove:OIL-1L", msg_id="i5"))
    assert "Removed OIL-1L" in r5["replies"][0]["text"]

    r6 = await post_webhook(client, build_interactive_webhook("cmd:clear", msg_id="i6"))
    assert "cleared" in r6["replies"][0]["text"]

    # Checkout after clear -> empty cart
    r7 = await post_webhook(client, build_interactive_webhook("cmd:checkout", msg_id="i7"))
    assert "empty" in r7["replies"][0]["text"]

    # Empty / unknown / malformed interactive payload is ignored
    assert (await post_webhook(client, build_interactive_webhook("unknown:id", msg_id="i8")))[
        "replies"
    ] == []
    assert (
        await post_webhook(client, build_interactive_webhook("add:OIL-1L:1:extra", msg_id="i8b"))
    )["replies"] == []
    empty_interactive = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "i9",
                                        "from": "254700111222",
                                        "type": "interactive",
                                        "interactive": {"type": "button_reply"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    assert (await post_webhook(client, empty_interactive))["replies"] == []


async def test_shop_selection_and_unresolved_merchant(
    client: AsyncClient, db: AsyncSession
) -> None:
    m1, _ = await seed_wholesaler(db, idx=1, phone="254711111111", with_products=False)
    m2, _ = await seed_wholesaler(db, idx=2, phone="254722222222", with_products=True)

    # With 2 merchants and non-matching display phone, customer is asked to select a shop
    r1 = await post_webhook(
        client, build_text_webhook("CATALOG", msg_id="s1", display_phone="invalid")
    )
    assert "SHOP <ID>" in r1["replies"][0]["text"]

    # Invalid SHOP syntax / unknown shop
    assert (
        "Usage: SHOP"
        in (
            await post_webhook(
                client, build_text_webhook("SHOP abc", msg_id="s2", display_phone="")
            )
        )["replies"][0]["text"]
    )
    assert (
        "Shop not found"
        in (
            await post_webhook(
                client, build_text_webhook("SHOP 999", msg_id="s3", display_phone="")
            )
        )["replies"][0]["text"]
    )

    # Connect to m1 (which has no products) via SHOP
    conn = await post_webhook(
        client, build_text_webhook(f"SHOP {m1.id}", msg_id="s4", display_phone="")
    )
    assert m1.name in conn["replies"][0]["text"]

    empty_cat = await post_webhook(
        client, build_text_webhook("CATALOG", msg_id="s5", display_phone="254799999999")
    )
    assert "No products in stock" in empty_cat["replies"][0]["text"]

    # If selected merchant is deleted later, fallback returns prompt cleanly
    await db.execute(text("DELETE FROM merchants WHERE id = :id"), {"id": m1.id})
    await db.commit()
    # Now only 1 merchant (m2) remains in DB -> single-merchant fallback picks m2
    cat_m2 = await post_webhook(
        client, build_text_webhook("CATALOG", msg_id="s6", display_phone="")
    )
    assert m2.name in cat_m2["replies"][0]["text"]


async def test_command_edge_cases_and_checkout_stock_race(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, [p1, p2, _] = await seed_wholesaler(db)

    # ADD with default qty=1, negative qty, invalid syntax, text REMOVE, and aliases (HI / PAY)
    hi_resp = await post_webhook(client, build_text_webhook("HI", msg_id="e_hi"))
    assert "OIL-1L" in hi_resp["replies"][0]["text"]

    default_qty = await post_webhook(client, build_text_webhook("ADD SUGAR-2KG", msg_id="e_def"))
    assert "Added 1 x SUGAR-2KG" in default_qty["replies"][0]["text"]

    rem_text = await post_webhook(client, build_text_webhook("REMOVE SUGAR-2KG", msg_id="e_rem_ok"))
    assert "Removed SUGAR-2KG" in rem_text["replies"][0]["text"]
    rem_missing = await post_webhook(
        client, build_text_webhook("REMOVE UNKNOWN", msg_id="e_rem_no")
    )
    assert "Removed UNKNOWN" in rem_missing["replies"][0]["text"]

    for i, cmd in enumerate(
        ["ADD", "ADD OIL-1L abc", "ADD OIL-1L 0", "ADD OIL-1L -1", "ADD UNKNOWN 1"]
    ):
        resp = await post_webhook(client, build_text_webhook(cmd, msg_id=f"e{i}"))
        text_out = resp["replies"][0]["text"]
        assert "Usage: ADD" in text_out or "not found" in text_out

    assert (
        "Usage: REMOVE"
        in (await post_webhook(client, build_text_webhook("REMOVE", msg_id="e_rem")))["replies"][0][
            "text"
        ]
    )
    assert (
        "Unknown command"
        in (await post_webhook(client, build_text_webhook("BLAH", msg_id="e_unk")))["replies"][0][
            "text"
        ]
    )

    # Inactive product in CART shows (unavailable), deleted product shows (unavailable) too
    await post_webhook(client, build_text_webhook("ADD OIL-1L 5", msg_id="e_add1"))
    await post_webhook(client, build_text_webhook("ADD SUGAR-2KG 1", msg_id="e_add2"))
    await db.execute(text("UPDATE products SET is_active = false WHERE id = :id"), {"id": p1.id})
    await db.execute(text("DELETE FROM products WHERE id = :id"), {"id": p2.id})
    await db.commit()

    cart_warn = await post_webhook(client, build_text_webhook("CART", msg_id="e_cart_warn"))
    assert "OIL-1L x 5 (unavailable)" in cart_warn["replies"][0]["text"]
    assert "SUGAR-2KG x 1 (unavailable)" in cart_warn["replies"][0]["text"]

    chk_missing = await post_webhook(client, build_text_webhook("PAY", msg_id="e_chk_miss"))
    assert "no longer available" in chk_missing["replies"][0]["text"]

    # Re-enable OIL-1L, clear, re-add OIL-1L 5, then drop stock behind the scenes
    await db.execute(text("UPDATE products SET is_active = true WHERE id = :id"), {"id": p1.id})
    await db.commit()

    # Clear, re-add OIL-1L 5, then drop stock behind the scenes to trigger reserve_stock failure
    await post_webhook(client, build_text_webhook("CLEAR", msg_id="e_clr"))
    await post_webhook(client, build_text_webhook("ADD OIL-1L 5", msg_id="e_add3"))
    await db.execute(text("UPDATE products SET stock_qty = 2 WHERE id = :id"), {"id": p1.id})
    await db.commit()

    chk_fail = await post_webhook(client, build_text_webhook("CHECKOUT", msg_id="e_chk_fail"))
    assert "Checkout failed" in chk_fail["replies"][0]["text"]


async def test_send_whatsapp_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unconfigured token/phone_number_id returns False without network call
    assert await send_whatsapp_text("254700111222", "Hello") is False

    monkeypatch.setattr(settings, "whatsapp_access_token", "fake-token")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "12345")

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        ok = await send_whatsapp_text("254700111222", "Order confirmed", http_client=mock_client)
    assert ok is True
    assert captured[0]["to"] == "254700111222"

    # Also exercise default client creation path (http_client=None)
    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(httpx, "AsyncClient", MockAsyncClient)
        assert await send_whatsapp_text("254700111222", "Default client") is True

    # Non-2xx response from Meta returns False cleanly
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="Server error"))
    ) as err_client:
        assert await send_whatsapp_text("254700111222", "Fail", http_client=err_client) is False

    # Network/transport error returns False cleanly without crashing caller
    def raise_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset")

    async with httpx.AsyncClient(transport=httpx.MockTransport(raise_transport)) as net_client:
        assert await send_whatsapp_text("254700111222", "Timeout", http_client=net_client) is False


async def test_catalog_truncates_at_30_items(client: AsyncClient, db: AsyncSession) -> None:
    merchant, _ = await seed_wholesaler(db, with_products=False)
    for i in range(32):
        db.add(
            Product(
                merchant_id=merchant.id,
                sku=f"ITEM-{i:02d}",
                name=f"Item {i:02d}",
                unit_price=Decimal("50.00"),
                stock_qty=10,
            )
        )
    await db.commit()

    resp = await post_webhook(client, build_text_webhook("CATALOG", msg_id="cat32"))
    text_out = resp["replies"][0]["text"]
    assert "ITEM-29" in text_out
    assert "ITEM-30" not in text_out
    assert "Showing first 30 items" in text_out


async def test_checkout_lock_blocks_concurrent_double_tap(
    client: AsyncClient, db: AsyncSession
) -> None:
    merchant, _ = await seed_wholesaler(db)
    await post_webhook(client, build_text_webhook("ADD OIL-1L 1", msg_id="lk1"))

    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.set(f"wa:lock:checkout:{merchant.id}:254700111222", "1", ex=30)
    finally:
        await r.aclose()

    resp = await post_webhook(client, build_text_webhook("CHECKOUT", msg_id="lk2"))
    assert "Checkout in progress" in resp["replies"][0]["text"]
    assert await db.scalar(text("SELECT count(*) FROM orders")) == 0


async def test_redis_flush_helper(client: AsyncClient) -> None:
    # Quick sanity check that Redis DB 15 responds to pings during tests
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        assert await r.ping() is True
    finally:
        await r.aclose()
