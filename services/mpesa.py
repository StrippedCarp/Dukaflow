"""Safaricom M-Pesa Daraja STK Push, callback processing, and reservation expiry."""

import base64
import logging
import secrets
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from models.order import Order, OrderStatus
from schemas.mpesa import StkCallback
from services.inventory import commit_stock, release_stock

logger = logging.getLogger(f"dukaflow.{__name__}")

OAUTH_CACHE_KEY = "mpesa:oauth:token"
OAUTH_CACHE_TTL_SECONDS = 3500


def verify_mpesa_token(provided_token: str | None, expected_secret: str) -> bool:
    """Verify the shared secret token on M-Pesa callback requests (fail-closed)."""
    cleaned = provided_token.strip() if provided_token else ""
    if not expected_secret or not cleaned:
        return False
    return secrets.compare_digest(cleaned.encode("utf-8"), expected_secret.encode("utf-8"))


def build_stk_password(shortcode: str, passkey: str, timestamp: str) -> str:
    """Generate the Base64-encoded Daraja STK Push password."""
    raw = f"{shortcode}{passkey}{timestamp}".encode()
    return base64.b64encode(raw).decode("ascii")


async def _daraja_request(
    method: str,
    url: str,
    http_client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> httpx.Response | None:
    """Execute a Daraja HTTP call, returning None cleanly on transport errors."""
    try:
        if http_client is not None:
            return await http_client.request(method, url, **kwargs)
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        logger.warning("Daraja HTTP error calling %s: %s", url, exc)
        return None


async def get_mpesa_access_token(
    redis: Redis, http_client: httpx.AsyncClient | None = None
) -> str | None:
    """Fetch a Daraja OAuth access token, caching it in Redis for ~58 minutes."""
    if not settings.mpesa_consumer_key or not settings.mpesa_consumer_secret:
        return None

    cached = await redis.get(OAUTH_CACHE_KEY)
    if cached:
        return str(cached)

    url = f"{settings.mpesa_base_url.rstrip('/')}/oauth/v1/generate?grant_type=client_credentials"
    resp = await _daraja_request(
        "GET",
        url,
        http_client=http_client,
        auth=(settings.mpesa_consumer_key, settings.mpesa_consumer_secret),
    )
    if resp is None or not resp.is_success:
        if resp is not None:
            logger.error("Daraja OAuth failed [%s]: %s", resp.status_code, resp.text)
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        return None

    await redis.set(OAUTH_CACHE_KEY, token, ex=OAUTH_CACHE_TTL_SECONDS)
    return token


async def initiate_stk_push(
    redis: Redis, order: Order, http_client: httpx.AsyncClient | None = None
) -> str | None:
    """Trigger an M-Pesa STK Push for ``order`` and return CheckoutRequestID on success."""
    if not settings.mpesa_passkey or not settings.mpesa_callback_url:
        return None

    amount_kes = int(order.total_amount.to_integral_value(rounding=ROUND_CEILING))
    if amount_kes < 1:
        return None

    token = await get_mpesa_access_token(redis, http_client=http_client)
    if token is None:
        return None

    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    password = build_stk_password(settings.mpesa_shortcode, settings.mpesa_passkey, timestamp)
    callback_url = settings.mpesa_callback_url
    if settings.mpesa_webhook_secret and "token=" not in callback_url:
        sep = "&" if "?" in callback_url else "?"
        callback_url = f"{callback_url}{sep}token={settings.mpesa_webhook_secret}"

    url = f"{settings.mpesa_base_url.rstrip('/')}/mpesa/stkpush/v1/processrequest"
    payload = {
        "BusinessShortCode": settings.mpesa_shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount_kes,
        "PartyA": order.customer_phone,
        "PartyB": settings.mpesa_shortcode,
        "PhoneNumber": order.customer_phone,
        "CallBackURL": callback_url,
        "AccountReference": f"DF-{order.id}",
        "TransactionDesc": f"DukaFlow Order #{order.id}",
    }
    resp = await _daraja_request(
        "POST",
        url,
        http_client=http_client,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp is None or not resp.is_success:
        if resp is not None:
            logger.error("Daraja STK Push failed [%s]: %s", resp.status_code, resp.text)
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    checkout_id = data.get("CheckoutRequestID") if isinstance(data, dict) else None
    return checkout_id if isinstance(checkout_id, str) and checkout_id else None


def collect_order_product_quantities(order: Order) -> dict[int, int]:
    """Sum requested quantities by product_id, skipping items whose product was deleted."""
    items_by_id: dict[int, int] = {}
    for item in order.items:
        if item.product_id is not None:
            items_by_id[item.product_id] = items_by_id.get(item.product_id, 0) + item.quantity
    return items_by_id


def format_merchant_paid_alert(order: Order) -> str:
    """Format the WhatsApp notification sent to the wholesaler when an order is paid."""
    lines = [
        f"New PAID Order #{order.id}!",
        f"Customer: {order.customer_phone}",
        f"M-Pesa Receipt: {order.mpesa_receipt_number}",
    ]
    for item in order.items:
        lines.append(f"- {item.sku} x {item.quantity} (KES {item.line_total:.2f})")
    lines.append(f"Total: KES {order.total_amount:.2f}")
    return "\n".join(lines)


async def process_stk_callback(db: AsyncSession, stk: StkCallback) -> tuple[Order | None, bool]:
    """Idempotently process a Daraja STK callback.

    Returns ``(order, transitioned)`` where ``transitioned`` is True only when
    this call moved the order out of ``PENDING``, so retries never send
    duplicate WhatsApp notifications.
    """
    stmt = (
        select(Order)
        .where(Order.checkout_request_id == stk.checkout_request_id)
        .options(selectinload(Order.items), selectinload(Order.merchant))
        .with_for_update()
    )
    order = await db.scalar(stmt)
    if order is None:
        return None, False
    if order.status != OrderStatus.PENDING:
        return order, False

    items_by_id = collect_order_product_quantities(order)

    meta = stk.metadata_dict()
    raw_amount = meta.get("Amount")
    try:
        paid_amount = Decimal(str(raw_amount)) if raw_amount is not None else Decimal("0.00")
    except InvalidOperation:
        paid_amount = Decimal("0.00")

    raw_receipt = meta.get("MpesaReceiptNumber")
    receipt = str(raw_receipt).strip() if raw_receipt is not None else ""

    if stk.result_code == 0 and paid_amount >= order.total_amount and receipt:
        if items_by_id:
            await commit_stock(db, order.merchant_id, items_by_id)
        order.status = OrderStatus.PAID
        order.mpesa_receipt_number = receipt
    else:
        if items_by_id:
            await release_stock(db, order.merchant_id, items_by_id)
        order.status = OrderStatus.FAILED

    order_id = order.id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.error("Duplicate MpesaReceiptNumber %s for Order #%s", receipt, order_id)
        return None, False

    await db.refresh(order, attribute_names=["items", "merchant", "updated_at"])
    return order, True


async def expire_stale_orders(db: AsyncSession, merchant_id: int, max_age_minutes: int = 15) -> int:
    """Cancel/fail abandoned PENDING orders older than ``max_age_minutes`` and free stock."""
    cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
    stmt = (
        select(Order)
        .where(
            Order.merchant_id == merchant_id,
            Order.status == OrderStatus.PENDING,
            Order.created_at <= cutoff,
        )
        .options(selectinload(Order.items))
        .order_by(Order.id)
        .with_for_update()
    )
    stale_orders = list((await db.scalars(stmt)).all())
    aggregated_items: dict[int, int] = {}
    for order in stale_orders:
        for product_id, qty in collect_order_product_quantities(order).items():
            aggregated_items[product_id] = aggregated_items.get(product_id, 0) + qty
        order.status = OrderStatus.FAILED

    if aggregated_items:
        # Lock and release all affected products once in ascending ID order to avoid deadlocks.
        await release_stock(db, merchant_id, aggregated_items)

    await db.commit()
    return len(stale_orders)


async def expire_all_stale_orders(db: AsyncSession, max_age_minutes: int = 15) -> int:
    """Expire abandoned PENDING orders older than ``max_age_minutes`` across all merchants."""
    cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
    merchant_ids_stmt = (
        select(Order.merchant_id)
        .where(Order.status == OrderStatus.PENDING, Order.created_at <= cutoff)
        .distinct()
        .order_by(Order.merchant_id)
    )
    merchant_ids = list((await db.scalars(merchant_ids_stmt)).all())
    total_expired = 0
    for merchant_id in merchant_ids:
        try:
            total_expired += await expire_stale_orders(db, merchant_id, max_age_minutes)
        except Exception:
            await db.rollback()
            logger.exception("Failed to expire stale orders for merchant_id=%s", merchant_id)
    return total_expired
