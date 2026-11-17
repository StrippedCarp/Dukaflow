"""Offline end-to-end terminal simulation of the DukaFlow order and M-Pesa lifecycle.

Uses the isolated ``dukaflow_test`` PostgreSQL database and Redis DB 15 so your
development database is never touched. Run from ``app/``:

    .\\venv\\Scripts\\python.exe scripts\\demo_simulation.py
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def _resolve_demo_db_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        url = make_url(explicit)
    else:
        dev_url = os.environ.get("DATABASE_URL") or dotenv_values(APP_DIR / ".env").get(
            "DATABASE_URL"
        )
        if not dev_url:
            raise RuntimeError("Set TEST_DATABASE_URL or DATABASE_URL in app/.env first.")
        url = make_url(dev_url).set(database="dukaflow_test")
    if not (url.database or "").endswith("_test"):
        raise RuntimeError("Refusing to run demo against non-test database.")
    return url.render_as_string(hide_password=False)


os.environ["DATABASE_URL"] = _resolve_demo_db_url()
os.environ["REDIS_URL"] = "redis://127.0.0.1:6380/15"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "demo-verify-value"  # noqa: S105
os.environ["WHATSAPP_APP_SECRET"] = "demo-app-signing-key"  # noqa: S105
os.environ["MPESA_WEBHOOK_SECRET"] = "demo-callback-key"  # noqa: S105
os.environ["LOG_LEVEL"] = "WARNING"

from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from redis import Redis as SyncRedis  # noqa: E402
from sqlalchemy import text  # noqa: E402

import routers.mpesa as mpesa_router  # noqa: E402
import routers.order as order_router  # noqa: E402
import routers.whatsapp as whatsapp_router  # noqa: E402
import services.whatsapp as whatsapp_service  # noqa: E402
from alembic import command  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import engine  # noqa: E402
from core.redis import redis_pool  # noqa: E402
from main import app  # noqa: E402
from models import Order  # noqa: E402


def _banner(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _print_whatsapp(sender: str, recipient: str, body: str) -> None:
    indented = "\n    ".join(body.splitlines())
    print(f"\n  [{sender} -> {recipient}]\n    {indented}")


def _prepare_clean_schema() -> None:
    cfg = Config(str(APP_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(APP_DIR / "alembic"))
    cfg.set_main_option(
        "sqlalchemy.url",
        os.environ["DATABASE_URL"].replace("postgresql+asyncpg", "postgresql+psycopg2"),
    )
    command.upgrade(cfg, "head")
    sync_redis = SyncRedis.from_url(settings.redis_url)
    try:
        sync_redis.flushdb()
    finally:
        sync_redis.close()


async def _reset_state() -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE order_items, orders, products, merchants RESTART IDENTITY CASCADE")
        )
    await engine.dispose()
    sync_redis = SyncRedis.from_url(settings.redis_url)
    try:
        sync_redis.flushdb()
    finally:
        sync_redis.close()
    await redis_pool.disconnect()


def _sign_wa_payload(text_body: str, msg_id: str) -> tuple[bytes, dict[str, str]]:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"display_phone_number": "254712345678"},
                            "messages": [
                                {
                                    "id": msg_id,
                                    "from": "254700111222",
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
    raw = json.dumps(payload).encode("utf-8")
    digest = hmac.new(settings.whatsapp_app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}
    return raw, headers


async def run_demo() -> None:
    """Execute the full merchant -> customer WhatsApp -> M-Pesa -> fulfillment flow."""
    _prepare_clean_schema()
    await _reset_state()

    async def mock_send_whatsapp_text(to_phone: str, body: str) -> bool:
        _print_whatsapp("DukaFlow Bot", to_phone, body)
        return True

    async def mock_initiate_stk_push(_redis: Any, order: Order) -> str:
        print(
            f"\n  [Daraja Simulator] Sent M-Pesa STK Push to {order.customer_phone} "
            f"for KES {order.total_amount:.2f} (CheckoutRequestID=ws_CO_DEMO_001)"
        )
        return "ws_CO_DEMO_001"

    cast(Any, whatsapp_router).send_whatsapp_text = mock_send_whatsapp_text
    cast(Any, mpesa_router).send_whatsapp_text = mock_send_whatsapp_text
    cast(Any, order_router).send_whatsapp_text = mock_send_whatsapp_text
    cast(Any, whatsapp_service).initiate_stk_push = mock_initiate_stk_push

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://demo") as client:
            _banner("STEP 1: Wholesaler Signup (POST /merchants/)")
            m_resp = await client.post(
                "/merchants/",
                json={
                    "name": "Wakulima Wholesale Ltd",
                    "email": "orders@wakulima.example.com",
                    "phone_number": "0712345678",
                },
            )
            merchant = m_resp.json()
            headers = {"X-API-Key": merchant["api_key"]}
            print(
                f"Registered Merchant #{merchant['id']}: {merchant['name']} "
                f"(WhatsApp={merchant['phone_number']})"
            )
            print(f"Issued API Key (shown once): {merchant['api_key'][:12]}...")

            _banner("STEP 2: Add Inventory Catalog (POST /products/)")
            catalog_items = [
                {
                    "sku": "OIL-1L",
                    "name": "Elianto Cooking Oil 1L",
                    "unit_price": "320.00",
                    "stock_qty": 50,
                },
                {
                    "sku": "SUGAR-2KG",
                    "name": "Kabras Sugar 2KG",
                    "unit_price": "280.00",
                    "stock_qty": 30,
                },
            ]
            for item in catalog_items:
                p_resp = await client.post("/products/", headers=headers, json=item)
                prod = p_resp.json()
                print(
                    f"  Added SKU {prod['sku']}: {prod['name']} | "
                    f"KES {prod['unit_price']} | Available={prod['available_qty']}"
                )

            _banner("STEP 3: Duka Customer Orders on WhatsApp (254700111222)")
            for idx, msg_text in enumerate(
                ["CATALOG", "ADD OIL-1L 2", "ADD SUGAR-2KG 1", "CART"], start=1
            ):
                _print_whatsapp("Duka Customer (254700111222)", "DukaFlow Bot", msg_text)
                raw, sig_headers = _sign_wa_payload(msg_text, f"wamid.demo.{idx}")
                await client.post("/webhooks/whatsapp", content=raw, headers=sig_headers)

            _banner("STEP 4: Customer Checks Out -> Stock Reserved & STK Push Sent")
            _print_whatsapp("Duka Customer (254700111222)", "DukaFlow Bot", "CHECKOUT")
            raw, sig_headers = _sign_wa_payload("CHECKOUT", "wamid.demo.checkout")
            chk_resp = await client.post("/webhooks/whatsapp", content=raw, headers=sig_headers)
            order_id = chk_resp.json()["replies"][0]["order_id"]
            prods_mid = (await client.get("/products/", headers=headers)).json()
            print(f"\n  [Postgres Check] Order #{order_id} status=pending (stock reserved):")
            for p in prods_mid:
                print(
                    f"    {p['sku']}: stock={p['stock_qty']}, "
                    f"reserved={p['reserved_qty']}, available={p['available_qty']}"
                )

            _banner("STEP 5: Customer Enters M-Pesa PIN -> Safaricom Daraja Callback")
            cb_payload = {
                "Body": {
                    "stkCallback": {
                        "MerchantRequestID": "29115-34620561-1",
                        "CheckoutRequestID": "ws_CO_DEMO_001",
                        "ResultCode": 0,
                        "ResultDesc": "The service request is processed successfully.",
                        "CallbackMetadata": {
                            "Item": [
                                {"Name": "Amount", "Value": "920.00"},
                                {"Name": "MpesaReceiptNumber", "Value": "QKL9DEMO77"},
                            ]
                        },
                    }
                }
            }
            cb_resp = await client.post(
                "/webhooks/mpesa/callback",
                params={"token": settings.mpesa_webhook_secret},
                json=cb_payload,
            )
            print(f"Callback Result: {cb_resp.json()}")
            prods_after = (await client.get("/products/", headers=headers)).json()
            for p in prods_after:
                print(
                    f"  [Postgres Committed] {p['sku']}: stock={p['stock_qty']}, "
                    f"reserved={p['reserved_qty']}, available={p['available_qty']}"
                )

            _banner(
                f"STEP 6: Wholesaler Dispatches Order #{order_id} (POST /orders/{order_id}/fulfill)"
            )
            fulfill_resp = await client.post(f"/orders/{order_id}/fulfill", headers=headers)
            print(f"Order #{order_id} new status: {fulfill_resp.json()['status']}")

            _banner("STEP 7: Sales Digest on WhatsApp & CSV Accounting Export")
            await client.post("/orders/summary/notify", headers=headers)
            csv_resp = await client.get("/orders/export.csv", headers=headers)
            print("\n  [Exported orders.csv]")
            for row in csv_resp.text.strip().splitlines():
                print(f"    {row}")
    finally:
        await _reset_state()
        _banner("DEMO COMPLETE - dukaflow_test & Redis DB 15 reset cleanly")


if __name__ == "__main__":
    asyncio.run(run_demo())
