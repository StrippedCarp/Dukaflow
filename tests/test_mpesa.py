import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.redis import redis_pool
from models import Order, OrderItem, OrderStatus, Product
from schemas.mpesa import StkCallback
from services.mpesa import (
    OAUTH_CACHE_KEY,
    build_stk_password,
    get_mpesa_access_token,
    initiate_stk_push,
    verify_mpesa_token,
)
from tests.test_orders import register_merchant_headers
from tests.test_whatsapp import build_text_webhook, post_webhook


def build_mpesa_callback(
    *,
    checkout_request_id: str = "ws_CO_123",
    result_code: int = 0,
    result_desc: str = "The service request is processed successfully.",
    amount: str | int | float | None = "640.00",
    receipt: str | None = "NLJ7RT61SV",
    include_metadata: bool = True,
) -> dict[str, Any]:
    stk: dict[str, Any] = {
        "MerchantRequestID": "29115-34620561-1",
        "CheckoutRequestID": checkout_request_id,
        "ResultCode": result_code,
        "ResultDesc": result_desc,
    }
    if include_metadata:
        items: list[dict[str, Any]] = []
        if amount is not None:
            items.append({"Name": "Amount", "Value": amount})
        if receipt is not None:
            items.append({"Name": "MpesaReceiptNumber", "Value": receipt})
        stk["CallbackMetadata"] = {"Item": items}
    return {"Body": {"stkCallback": stk}}


def configure_mpesa(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    defaults = {
        "mpesa_base_url": "https://sandbox.safaricom.co.ke",
        "mpesa_consumer_key": "consumer-key",
        "mpesa_consumer_secret": "consumer-secret",
        "mpesa_shortcode": "174379",
        "mpesa_passkey": "test-passkey",
        "mpesa_callback_url": "https://example.com/webhooks/mpesa/callback",
    }
    defaults.update(overrides)
    for key, val in defaults.items():
        monkeypatch.setattr(settings, key, val)


async def test_mpesa_callback_security_and_validation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = build_mpesa_callback()

    assert verify_mpesa_token("abc", "") is False
    assert verify_mpesa_token(None, "secret") is False
    assert verify_mpesa_token("  secret ", "secret") is True

    # Missing / wrong token -> 403
    assert (await client.post("/webhooks/mpesa/callback", json=payload)).status_code == 403
    assert (
        await client.post("/webhooks/mpesa/callback", params={"token": "wrong"}, json=payload)
    ).status_code == 403

    # Empty configured secret fails closed at HTTP layer even if ?token="" matches
    with monkeypatch.context() as m:
        m.setattr(settings, "mpesa_webhook_secret", "")
        assert (
            await client.post("/webhooks/mpesa/callback", params={"token": ""}, json=payload)
        ).status_code == 403

    # Oversized -> 413
    huge = b"x" * 262_145
    assert (
        await client.post(
            "/webhooks/mpesa/callback",
            params={"token": settings.mpesa_webhook_secret},
            content=huge,
        )
    ).status_code == 413

    # Malformed JSON -> 422
    assert (
        await client.post(
            "/webhooks/mpesa/callback",
            params={"token": settings.mpesa_webhook_secret},
            content=b"{bad-json",
        )
    ).status_code == 422

    # Valid header token with unknown CheckoutRequestID -> 200 Accepted
    ok = await client.post(
        "/webhooks/mpesa/callback",
        headers={"X-Mpesa-Token": settings.mpesa_webhook_secret},
        json=payload,
    )
    assert ok.status_code == 200
    assert ok.json() == {
        "ResultCode": 0,
        "ResultDesc": "Accepted",
        "order_id": None,
        "order_status": None,
    }


async def test_build_stk_password_and_metadata_dict() -> None:
    pw = build_stk_password("174379", "pass", "20261116140000")
    assert base64.b64decode(pw).decode() == "174379pass20261116140000"

    stk = StkCallback(CheckoutRequestID="ws_1", ResultCode=1032)
    assert stk.metadata_dict() == {}


async def test_get_mpesa_access_token_caching_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = Redis(connection_pool=redis_pool)
    try:
        # Unconfigured credentials -> None
        assert await get_mpesa_access_token(r) is None

        configure_mpesa(monkeypatch)
        calls = 0

        def ok_handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"access_token": "tok-123"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as c:
            assert await get_mpesa_access_token(r, http_client=c) == "tok-123"
            # Second call reads from Redis without HTTP request
            assert await get_mpesa_access_token(r, http_client=c) == "tok-123"
        assert calls == 1

        # Clear cache and test error paths: HTTP 500, invalid JSON, missing token, transport error
        await r.delete(OAUTH_CACHE_KEY)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(500, text="err"))
        ) as err_c:
            assert await get_mpesa_access_token(r, http_client=err_c) is None

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text="not-json"))
        ) as bad_json_c:
            assert await get_mpesa_access_token(r, http_client=bad_json_c) is None

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        ) as empty_c:
            assert await get_mpesa_access_token(r, http_client=empty_c) is None

        def raise_err(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        async with httpx.AsyncClient(transport=httpx.MockTransport(raise_err)) as net_c:
            assert await get_mpesa_access_token(r, http_client=net_c) is None

        # Exercise default httpx.AsyncClient creation when http_client=None
        class MockClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(ok_handler)
                super().__init__(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(httpx, "AsyncClient", MockClient)
            assert await get_mpesa_access_token(r) == "tok-123"
    finally:
        await r.aclose()


async def test_initiate_stk_push_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    r = Redis(connection_pool=redis_pool)
    order = Order(
        id=42,
        merchant_id=1,
        customer_phone="254700111222",
        total_amount=Decimal("640.00"),
    )
    try:
        # Unconfigured passkey/callback -> None
        assert await initiate_stk_push(r, order) is None

        configure_mpesa(
            monkeypatch,
            mpesa_callback_url="https://example.com/webhooks/mpesa/callback?src=df",
        )
        # Zero amount -> None
        zero_order = Order(
            id=43, merchant_id=1, customer_phone="254700111222", total_amount=Decimal("0.00")
        )
        assert await initiate_stk_push(r, zero_order) is None

        # OAuth failure -> None
        monkeypatch.setattr(settings, "mpesa_consumer_key", "")
        assert await initiate_stk_push(r, order) is None
        monkeypatch.setattr(settings, "mpesa_consumer_key", "consumer-key")

        # Seed cached token, then test STK Push failure, bad JSON, and success with &token=
        await r.set(OAUTH_CACHE_KEY, "cached-tok", ex=300)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(500, text="stk err"))
        ) as fail_c:
            assert await initiate_stk_push(r, order, http_client=fail_c) is None

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text="not-json"))
        ) as bad_json_c:
            assert await initiate_stk_push(r, order, http_client=bad_json_c) is None

        def raise_stk_err(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("stk timeout")

        async with httpx.AsyncClient(transport=httpx.MockTransport(raise_stk_err)) as net_c:
            assert await initiate_stk_push(r, order, http_client=net_c) is None

        captured_callbacks: list[str] = []

        def stk_ok(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode())
            captured_callbacks.append(body["CallBackURL"])
            return httpx.Response(200, json={"CheckoutRequestID": "ws_CO_999"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(stk_ok)) as ok_c:
            cid = await initiate_stk_push(r, order, http_client=ok_c)
            monkeypatch.setattr(
                settings,
                "mpesa_callback_url",
                "https://example.com/webhooks/mpesa/callback?token=preset",
            )
            cid2 = await initiate_stk_push(r, order, http_client=ok_c)
        assert cid == "ws_CO_999"
        assert cid2 == "ws_CO_999"
        assert "&token=test-mpesa-webhook-secret" in captured_callbacks[0]
        assert captured_callbacks[1].endswith("token=preset")
    finally:
        await r.aclose()


async def test_whatsapp_checkout_triggers_stk_push_and_callback_commits_stock(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, h1 = await register_merchant_headers(client, 1)
    p_resp = await client.post(
        "/products/",
        headers=h1,
        json={"sku": "OIL-1L", "name": "Oil 1L", "unit_price": "320.00", "stock_qty": 10},
    )
    pid = p_resp.json()["id"]

    configure_mpesa(monkeypatch)

    def daraja_handler(req: httpx.Request) -> httpx.Response:
        if "oauth" in str(req.url):
            return httpx.Response(200, json={"access_token": "tok-live"})
        return httpx.Response(200, json={"CheckoutRequestID": "ws_CO_WA_001"})

    class MockDarajaClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(daraja_handler)
            super().__init__(*args, **kwargs)

    await post_webhook(
        client, build_text_webhook("ADD OIL-1L 2", msg_id="m_add", display_phone="254712345671")
    )
    with monkeypatch.context() as m:
        m.setattr(httpx, "AsyncClient", MockDarajaClient)
        chk = await post_webhook(
            client, build_text_webhook("CHECKOUT", msg_id="m_chk", display_phone="254712345671")
        )

    reply = chk["replies"][0]
    order_id = reply["order_id"]
    assert "Enter your M-Pesa PIN" in reply["text"]

    order_data = (await client.get(f"/orders/{order_id}", headers=h1)).json()
    assert order_data["checkout_request_id"] == "ws_CO_WA_001"

    sent_notifications: list[tuple[str, str]] = []

    async def fake_send_wa(to_phone: str, body: str) -> bool:
        sent_notifications.append((to_phone, body))
        return True

    monkeypatch.setattr("routers.mpesa.send_whatsapp_text", fake_send_wa)

    # Successful callback commits reserved stock, stores receipt, and notifies customer + merchant
    cb_resp = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(
            checkout_request_id="ws_CO_WA_001", amount="640.00", receipt="NLJ7RT61SV"
        ),
    )
    assert cb_resp.status_code == 200
    assert cb_resp.json()["order_status"] == "paid"
    assert len(sent_notifications) == 2
    assert sent_notifications[0][0] == "254700111222"
    assert "NLJ7RT61SV" in sent_notifications[0][1]
    assert sent_notifications[1][0] == "254712345671"
    assert "New PAID Order" in sent_notifications[1][1]
    assert "NLJ7RT61SV" in sent_notifications[1][1]
    assert "OIL-1L x 2" in sent_notifications[1][1]

    order_after = (await client.get(f"/orders/{order_id}", headers=h1)).json()
    assert order_after["mpesa_receipt_number"] == "NLJ7RT61SV"

    prod = (await client.get(f"/products/{pid}", headers=h1)).json()
    assert prod["stock_qty"] == 8
    assert prod["reserved_qty"] == 0
    assert prod["available_qty"] == 8

    # Duplicate callback is idempotent AND does NOT send duplicate WhatsApp alerts
    dup_resp = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(
            checkout_request_id="ws_CO_WA_001", amount="640.00", receipt="NLJ7RT61SV"
        ),
    )
    assert dup_resp.status_code == 200
    assert dup_resp.json()["order_status"] == "paid"
    assert len(sent_notifications) == 2
    prod_after = (await client.get(f"/products/{pid}", headers=h1)).json()
    assert prod_after["stock_qty"] == 8


async def test_callback_failure_underpayment_and_deleted_product(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent_failures: list[tuple[str, str]] = []

    async def fake_send_wa(to_phone: str, body: str) -> bool:
        sent_failures.append((to_phone, body))
        return True

    monkeypatch.setattr("routers.mpesa.send_whatsapp_text", fake_send_wa)

    m1_id, h1 = await register_merchant_headers(client, 1)
    p = Product(
        merchant_id=m1_id,
        sku="SUGAR",
        name="Sugar 2KG",
        unit_price=Decimal("300.00"),
        stock_qty=10,
        reserved_qty=8,
    )
    db.add(p)
    await db.flush()

    # Order 1: underpaid callback (ResultCode 0, Amount 100 < 600) -> FAILED, stock released
    o1 = Order(
        merchant_id=m1_id,
        customer_phone="254700000001",
        total_amount=Decimal("600.00"),
        checkout_request_id="ws_CO_UNDER",
        items=[
            OrderItem(
                product_id=p.id,
                sku=p.sku,
                product_name=p.name,
                quantity=2,
                unit_price=p.unit_price,
            )
        ],
    )
    # Order 2: cancelled/failed ResultCode=1032 with invalid Amount string -> FAILED, stock released
    o2 = Order(
        merchant_id=m1_id,
        customer_phone="254700000002",
        total_amount=Decimal("600.00"),
        checkout_request_id="ws_CO_FAIL",
        items=[
            OrderItem(
                product_id=p.id,
                sku=p.sku,
                product_name=p.name,
                quantity=2,
                unit_price=p.unit_price,
            )
        ],
    )
    # Order 3: already cancelled order receiving late callback -> stays cancelled
    o3 = Order(
        merchant_id=m1_id,
        customer_phone="254700000003",
        total_amount=Decimal("300.00"),
        status=OrderStatus.CANCELLED,
        checkout_request_id="ws_CO_CANCELLED",
    )
    # Order 4: paid callback with product_id=None (deleted product) -> PAID cleanly
    o4 = Order(
        merchant_id=m1_id,
        customer_phone="254700000004",
        total_amount=Decimal("300.00"),
        checkout_request_id="ws_CO_NO_PROD",
        items=[
            OrderItem(
                product_id=None,
                sku="OLD",
                product_name="Old",
                quantity=1,
                unit_price=Decimal("300.00"),
            )
        ],
    )
    # Order 5: failed callback with product_id=None -> FAILED cleanly
    o5 = Order(
        merchant_id=m1_id,
        customer_phone="254700000005",
        total_amount=Decimal("300.00"),
        checkout_request_id="ws_CO_NO_PROD_FAIL",
        items=[
            OrderItem(
                product_id=None,
                sku="OLD",
                product_name="Old",
                quantity=1,
                unit_price=Decimal("300.00"),
            )
        ],
    )
    # Order 6: ResultCode 0 with missing MpesaReceiptNumber -> FAILED (cannot bypass unique index)
    o6 = Order(
        merchant_id=m1_id,
        customer_phone="254700000006",
        total_amount=Decimal("600.00"),
        checkout_request_id="ws_CO_NO_REC",
        items=[
            OrderItem(
                product_id=p.id,
                sku=p.sku,
                product_name=p.name,
                quantity=2,
                unit_price=p.unit_price,
            )
        ],
    )
    # Order 7: duplicate MpesaReceiptNumber collision across distinct orders -> handled cleanly
    o7 = Order(
        merchant_id=m1_id,
        customer_phone="254700000007",
        total_amount=Decimal("300.00"),
        checkout_request_id="ws_CO_DUP_REC",
    )
    db.add_all([o1, o2, o3, o4, o5, o6, o7])
    await db.commit()

    r1 = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(checkout_request_id="ws_CO_UNDER", amount="100.00"),
    )
    assert r1.json()["order_status"] == "failed"

    r2 = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(
            checkout_request_id="ws_CO_FAIL", result_code=1032, amount="not-a-number"
        ),
    )
    assert r2.json()["order_status"] == "failed"

    r3 = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(checkout_request_id="ws_CO_CANCELLED", amount="300.00"),
    )
    assert r3.json()["order_status"] == "cancelled"

    r4 = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(
            checkout_request_id="ws_CO_NO_PROD", amount="300.00", receipt="REC_NOPROD"
        ),
    )
    assert r4.json()["order_status"] == "paid"

    r5 = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(
            checkout_request_id="ws_CO_NO_PROD_FAIL", result_code=1032, include_metadata=False
        ),
    )
    assert r5.json()["order_status"] == "failed"

    r6 = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(
            checkout_request_id="ws_CO_NO_REC", amount="600.00", receipt=None
        ),
    )
    assert r6.json()["order_status"] == "failed"

    # Replaying REC_NOPROD (already on o4) against o7 hits IntegrityError -> safe rollback, no 500
    r7 = await client.post(
        "/webhooks/mpesa/callback",
        params={"token": settings.mpesa_webhook_secret},
        json=build_mpesa_callback(
            checkout_request_id="ws_CO_DUP_REC", amount="300.00", receipt="REC_NOPROD"
        ),
    )
    assert r7.status_code == 200
    assert r7.json()["order_id"] is None
    assert (await client.get(f"/orders/{o7.id}", headers=h1)).json()["status"] == "pending"

    assert any("Payment failed for Order" in msg for _, msg in sent_failures)
    prod = (await client.get(f"/products/{p.id}", headers=h1)).json()
    assert prod["reserved_qty"] == 2  # 8 - 2 (o1) - 2 (o2) - 2 (o6)
    assert prod["available_qty"] == 8


async def test_manual_stk_push_and_expire_stale_orders(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    m1_id, h1 = await register_merchant_headers(client, 1)
    _, h2 = await register_merchant_headers(client, 2)

    p = Product(
        merchant_id=m1_id,
        sku="FLOUR",
        name="Maize Flour 2KG",
        unit_price=Decimal("200.00"),
        stock_qty=10,
        reserved_qty=3,
    )
    db.add(p)
    await db.flush()

    stale_order = Order(
        merchant_id=m1_id,
        customer_phone="254700000010",
        total_amount=Decimal("600.00"),
        items=[
            OrderItem(
                product_id=p.id,
                sku=p.sku,
                product_name=p.name,
                quantity=3,
                unit_price=p.unit_price,
            )
        ],
    )
    stale_no_prod = Order(
        merchant_id=m1_id,
        customer_phone="254700000011",
        total_amount=Decimal("100.00"),
        items=[
            OrderItem(
                product_id=None,
                sku="OLD",
                product_name="Old",
                quantity=1,
                unit_price=Decimal("100.00"),
            )
        ],
    )
    fresh_order = Order(
        merchant_id=m1_id,
        customer_phone="254700000012",
        total_amount=Decimal("200.00"),
    )
    paid_order = Order(
        merchant_id=m1_id,
        customer_phone="254700000013",
        total_amount=Decimal("200.00"),
        status=OrderStatus.PAID,
    )
    db.add_all([stale_order, stale_no_prod, fresh_order, paid_order])
    await db.commit()

    # Manual STK push: 404 other merchant, 409 PAID, 502 unconfigured, 200 ok
    assert (await client.post(f"/orders/{fresh_order.id}/stk-push", headers=h2)).status_code == 404
    assert (await client.post(f"/orders/{paid_order.id}/stk-push", headers=h1)).status_code == 409
    assert (await client.post(f"/orders/{fresh_order.id}/stk-push", headers=h1)).status_code == 502

    configure_mpesa(monkeypatch)

    class MockDaraja(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            def handler(req: httpx.Request) -> httpx.Response:
                if "oauth" in str(req.url):
                    return httpx.Response(200, json={"access_token": "tok-manual"})
                return httpx.Response(200, json={"CheckoutRequestID": "ws_CO_MANUAL"})

            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(httpx, "AsyncClient", MockDaraja)
        ok_push = await client.post(f"/orders/{fresh_order.id}/stk-push", headers=h1)
    assert ok_push.status_code == 200
    assert ok_push.json()["checkout_request_id"] == "ws_CO_MANUAL"

    # STK lock contention -> 409; concurrent status flip during STK push -> 409
    r = Redis(connection_pool=redis_pool)
    try:
        await r.set(f"mpesa:lock:stk:{fresh_order.id}", "1", ex=30)
        assert (
            await client.post(f"/orders/{fresh_order.id}/stk-push", headers=h1)
        ).status_code == 409
        await r.delete(f"mpesa:lock:stk:{fresh_order.id}")
    finally:
        await r.aclose()

    async def flip_status_while_pushing(*args: Any, **kwargs: Any) -> str:
        await db.execute(
            text("UPDATE orders SET status = 'cancelled' WHERE id = :id"),
            {"id": fresh_order.id},
        )
        await db.commit()
        return "ws_CO_RACING"

    with monkeypatch.context() as m:
        m.setattr("routers.order.initiate_stk_push", flip_status_while_pushing)
        assert (
            await client.post(f"/orders/{fresh_order.id}/stk-push", headers=h1)
        ).status_code == 409

    # Backdate stale_order and stale_no_prod by 20 minutes; reset fresh_order back to pending
    old_ts = datetime.now(UTC) - timedelta(minutes=20)
    await db.execute(
        text("UPDATE orders SET status = 'pending' WHERE id = :id"), {"id": fresh_order.id}
    )
    await db.execute(
        text("UPDATE orders SET created_at = :ts WHERE id IN (:id1, :id2)"),
        {"ts": old_ts, "id1": stale_order.id, "id2": stale_no_prod.id},
    )
    await db.commit()

    # Query bounds and tenant isolation on expire-stale
    assert (
        await client.post("/orders/expire-stale", headers=h1, params={"max_age_minutes": 0})
    ).status_code == 422
    assert (
        await client.post("/orders/expire-stale", headers=h1, params={"max_age_minutes": 10081})
    ).status_code == 422
    assert (await client.post("/orders/expire-stale", headers=h2)).json() == {"expired_count": 0}

    exp = await client.post("/orders/expire-stale", headers=h1)
    assert exp.status_code == 200
    assert exp.json() == {"expired_count": 2}

    prod = (await client.get(f"/products/{p.id}", headers=h1)).json()
    assert prod["reserved_qty"] == 0
    assert prod["available_qty"] == 10
    assert (await client.get(f"/orders/{fresh_order.id}", headers=h1)).json()["status"] == "pending"
    assert (await client.get(f"/orders/{stale_order.id}", headers=h1)).json()["status"] == "failed"
