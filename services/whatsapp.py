"""WhatsApp Cloud API signature verification, deduplication, cart state, and order flow."""

import hashlib
import hmac
import json
import logging
import secrets
from decimal import Decimal

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.phone import normalize_kenyan_phone
from models.merchant import Merchant
from models.order import Order, OrderItem, OrderStatus
from models.product import Product
from schemas.whatsapp import WebhookReply, WhatsAppMessage
from services.inventory import InsufficientStockError, ProductNotFoundError, reserve_stock
from services.mpesa import initiate_stk_push

logger = logging.getLogger(f"dukaflow.{__name__}")

DEDUPE_TTL_SECONDS = 86400
CART_TTL_SECONDS = 86400
CHECKOUT_LOCK_TTL_SECONDS = 30
CATALOG_MAX_ITEMS = 30

_SIMPLE_INTERACTIVE_MAP = {
    "cmd:catalog": "CATALOG",
    "cmd:cart": "CART",
    "cmd:clear": "CLEAR",
    "cmd:checkout": "CHECKOUT",
}


def verify_whatsapp_signature(
    raw_body: bytes, signature_header: str | None, app_secret: str
) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` HMAC header (fail-closed)."""
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
    return secrets.compare_digest(signature_header.encode("utf-8"), expected.encode("utf-8"))


async def mark_message_processed(redis: Redis, message_id: str) -> bool:
    """Record ``message_id`` in Redis once; return ``False`` if already seen."""
    result = await redis.set(f"wa:msg:{message_id}", "1", nx=True, ex=DEDUPE_TTL_SECONDS)
    return bool(result)


async def get_selected_merchant_id(redis: Redis, customer_phone: str) -> int | None:
    """Return the merchant ID currently selected by ``customer_phone``, if any."""
    raw = await redis.get(f"wa:merchant:{customer_phone}")
    return int(raw) if raw is not None else None


async def set_selected_merchant_id(redis: Redis, customer_phone: str, merchant_id: int) -> None:
    """Store the customer's selected wholesaler in Redis."""
    await redis.set(f"wa:merchant:{customer_phone}", str(merchant_id), ex=CART_TTL_SECONDS)


async def get_cart(redis: Redis, merchant_id: int, customer_phone: str) -> dict[str, int]:
    """Load the customer's ``{sku: qty}`` cart for ``merchant_id`` from Redis."""
    raw = await redis.get(f"wa:cart:{merchant_id}:{customer_phone}")
    if not raw:
        return {}
    data: dict[str, int] = json.loads(raw)
    return {str(k): int(v) for k, v in data.items()}


async def save_cart(
    redis: Redis, merchant_id: int, customer_phone: str, cart: dict[str, int]
) -> None:
    """Save the customer's cart to Redis, or delete the key if empty."""
    key = f"wa:cart:{merchant_id}:{customer_phone}"
    if not cart:
        await redis.delete(key)
    else:
        await redis.set(key, json.dumps(cart), ex=CART_TTL_SECONDS)


def extract_command_text(msg: WhatsAppMessage) -> str:
    """Convert a text or interactive WhatsApp message into a canonical command string."""
    if msg.type == "text" and msg.text is not None:
        return msg.text.body.strip()

    if msg.type == "interactive" and msg.interactive is not None:
        reply_item = msg.interactive.button_reply or msg.interactive.list_reply
        if reply_item is None:
            return ""
        reply_id = reply_item.id.strip()
        if reply_id in _SIMPLE_INTERACTIVE_MAP:
            return _SIMPLE_INTERACTIVE_MAP[reply_id]
        if reply_id.startswith("add:"):
            parts = reply_id.split(":")
            if len(parts) == 3:
                return f"ADD {parts[1]} {parts[2]}"
            if len(parts) == 2:
                return f"ADD {parts[1]} 1"
        if reply_id.startswith("remove:"):
            sku = reply_id.split(":", 1)[1]
            return f"REMOVE {sku}"
    return ""


async def resolve_merchant(
    db: AsyncSession, redis: Redis, customer_phone: str, display_phone: str
) -> Merchant | None:
    """Identify which wholesaler the customer is ordering from."""
    if display_phone:
        try:
            normalized_display = normalize_kenyan_phone(display_phone)
        except ValueError:
            normalized_display = ""
        if normalized_display:
            merchant = await db.scalar(
                select(Merchant).where(Merchant.phone_number == normalized_display)
            )
            if merchant is not None:
                return merchant

    selected_id = await get_selected_merchant_id(redis, customer_phone)
    if selected_id is not None:
        merchant = await db.get(Merchant, selected_id)
        if merchant is not None:
            return merchant

    merchants = list((await db.scalars(select(Merchant).order_by(Merchant.id).limit(2))).all())
    if len(merchants) == 1:
        return merchants[0]
    return None


async def _build_catalog_reply(db: AsyncSession, merchant: Merchant) -> str:
    stmt = (
        select(Product)
        .where(
            Product.merchant_id == merchant.id,
            Product.is_active.is_(True),
            (Product.stock_qty - Product.reserved_qty) > 0,
        )
        .order_by(Product.id)
        .limit(CATALOG_MAX_ITEMS + 1)
    )
    products = list((await db.scalars(stmt)).all())
    if not products:
        return f"{merchant.name}: No products in stock right now."

    truncated = len(products) > CATALOG_MAX_ITEMS
    visible = products[:CATALOG_MAX_ITEMS]

    lines = [f"{merchant.name} Catalog:"]
    for p in visible:
        lines.append(f"- {p.sku}: {p.name} @ KES {p.unit_price:.2f} ({p.available_qty} avail)")
    if truncated:
        lines.append(f"(Showing first {CATALOG_MAX_ITEMS} items.)")
    lines.append("Reply ADD <SKU> <QTY>, CART, or CHECKOUT.")
    return "\n".join(lines)


async def _handle_add(
    db: AsyncSession, redis: Redis, merchant: Merchant, customer_phone: str, parts: list[str]
) -> str:
    if len(parts) not in (2, 3):
        return "Usage: ADD <SKU> <QTY> (e.g. ADD OIL-1L 2)"
    sku = parts[1].upper()
    qty = 1
    if len(parts) == 3:
        try:
            qty = int(parts[2])
        except ValueError:
            return "Usage: ADD <SKU> <QTY> (e.g. ADD OIL-1L 2)"
    if qty <= 0:
        return "Usage: ADD <SKU> <QTY> (e.g. ADD OIL-1L 2)"

    product = await db.scalar(
        select(Product).where(Product.merchant_id == merchant.id, Product.sku == sku)
    )
    if product is None or not product.is_active:
        return f"Product {sku} not found."

    cart = await get_cart(redis, merchant.id, customer_phone)
    new_qty = cart.get(sku, 0) + qty
    if new_qty > product.available_qty:
        return f"Only {product.available_qty} units of {sku} available."

    cart[sku] = new_qty
    await save_cart(redis, merchant.id, customer_phone, cart)
    return (
        f"Added {qty} x {product.sku} ({product.name}). "
        f"Cart now has {new_qty} x {product.sku}. Reply CART or CHECKOUT."
    )


async def _handle_remove(
    redis: Redis, merchant: Merchant, customer_phone: str, parts: list[str]
) -> str:
    if len(parts) != 2:
        return "Usage: REMOVE <SKU>"
    sku = parts[1].upper()
    cart = await get_cart(redis, merchant.id, customer_phone)
    cart.pop(sku, None)
    await save_cart(redis, merchant.id, customer_phone, cart)
    return f"Removed {sku} from your cart."


async def _build_cart_reply(
    db: AsyncSession, redis: Redis, merchant: Merchant, customer_phone: str
) -> str:
    cart = await get_cart(redis, merchant.id, customer_phone)
    if not cart:
        return "Your cart is empty. Reply CATALOG to browse products."

    products = list(
        (
            await db.scalars(
                select(Product)
                .where(Product.merchant_id == merchant.id, Product.sku.in_(sorted(cart)))
                .order_by(Product.id)
            )
        ).all()
    )
    by_sku = {p.sku: p for p in products}
    lines = [f"{merchant.name} Cart:"]
    total = Decimal("0.00")
    for sku, qty in cart.items():
        product = by_sku.get(sku)
        if product is None or not product.is_active:
            lines.append(f"- {sku} x {qty} (unavailable)")
            continue
        line_total = product.unit_price * qty
        total += line_total
        lines.append(
            f"- {product.sku} x {qty} @ KES {product.unit_price:.2f} = KES {line_total:.2f}"
        )
    lines.append(f"Total: KES {total:.2f}")
    lines.append("Reply CHECKOUT to place order or CLEAR to empty.")
    return "\n".join(lines)


async def _handle_checkout(
    db: AsyncSession, redis: Redis, merchant: Merchant, customer_phone: str
) -> WebhookReply:
    # Prevent double-tap CHECKOUT (two concurrent wamids) from creating duplicate orders.
    lock_key = f"wa:lock:checkout:{merchant.id}:{customer_phone}"
    if not await redis.set(lock_key, "1", nx=True, ex=CHECKOUT_LOCK_TTL_SECONDS):
        return WebhookReply(to=customer_phone, text="Checkout in progress, please wait...")

    try:
        cart = await get_cart(redis, merchant.id, customer_phone)
        if not cart:
            return WebhookReply(
                to=customer_phone, text="Your cart is empty. Reply CATALOG to browse products."
            )

        products = list(
            (
                await db.scalars(
                    select(Product)
                    .where(Product.merchant_id == merchant.id, Product.sku.in_(sorted(cart)))
                    .order_by(Product.id)
                )
            ).all()
        )
        if len(products) != len(cart):
            return WebhookReply(
                to=customer_phone,
                text="Some items in your cart are no longer available. Please CLEAR and try again.",
            )

        items_by_id = {p.id: cart[p.sku] for p in products}
        try:
            locked_products = await reserve_stock(db, merchant.id, items_by_id)
        except (ProductNotFoundError, InsufficientStockError) as exc:
            await db.rollback()
            return WebhookReply(to=customer_phone, text=f"Checkout failed: {exc}")

        total_amount = sum(
            (p.unit_price * cart[p.sku] for p in locked_products),
            Decimal("0.00"),
        )
        order = Order(
            merchant_id=merchant.id,
            customer_phone=customer_phone,
            total_amount=total_amount,
            status=OrderStatus.PENDING,
            items=[
                OrderItem(
                    product_id=p.id,
                    sku=p.sku,
                    product_name=p.name,
                    quantity=cart[p.sku],
                    unit_price=p.unit_price,
                )
                for p in locked_products
            ],
        )
        db.add(order)
        await db.commit()
        await db.refresh(order)
        await save_cart(redis, merchant.id, customer_phone, {})
        checkout_id = await initiate_stk_push(redis, order)
        reply_text = (
            f"Order #{order.id} placed! Total: KES {order.total_amount:.2f}. "
            "Stock reserved — awaiting M-Pesa payment."
        )
        if checkout_id is not None:
            order.checkout_request_id = checkout_id
            await db.commit()
            reply_text = (
                f"Order #{order.id} placed! Total: KES {order.total_amount:.2f}. "
                "Enter your M-Pesa PIN on your phone to complete payment."
            )
        return WebhookReply(to=customer_phone, order_id=order.id, text=reply_text)
    finally:
        await redis.delete(lock_key)


async def handle_incoming_message(
    db: AsyncSession,
    redis: Redis,
    msg: WhatsAppMessage,
    display_phone: str,
) -> WebhookReply | None:
    """Process one incoming WhatsApp message and return the reply to send, if any."""
    try:
        customer_phone = normalize_kenyan_phone(msg.from_)
    except ValueError:
        return None

    command_text = extract_command_text(msg)
    if not command_text:
        return None

    if not await mark_message_processed(redis, msg.id):
        return None

    parts = command_text.split()
    verb = parts[0].upper()

    if verb == "SHOP":
        if len(parts) != 2 or not parts[1].isdigit():
            return WebhookReply(to=customer_phone, text="Usage: SHOP <ID> (e.g. SHOP 1)")
        merchant_obj = await db.get(Merchant, int(parts[1]))
        if merchant_obj is None:
            return WebhookReply(to=customer_phone, text="Shop not found.")
        await set_selected_merchant_id(redis, customer_phone, merchant_obj.id)
        return WebhookReply(
            to=customer_phone,
            text=f"Connected to {merchant_obj.name}. Reply CATALOG to view products.",
        )

    merchant = await resolve_merchant(db, redis, customer_phone, display_phone)
    if merchant is None:
        return WebhookReply(
            to=customer_phone,
            text="Welcome to DukaFlow! Reply SHOP <ID> to select a wholesaler.",
        )

    if verb in {"HI", "HELLO", "HABARI", "START", "MENU", "CATALOG", "LIST"}:
        return WebhookReply(to=customer_phone, text=await _build_catalog_reply(db, merchant))
    if verb == "ADD":
        return WebhookReply(
            to=customer_phone, text=await _handle_add(db, redis, merchant, customer_phone, parts)
        )
    if verb == "REMOVE":
        return WebhookReply(
            to=customer_phone, text=await _handle_remove(redis, merchant, customer_phone, parts)
        )
    if verb == "CLEAR":
        await save_cart(redis, merchant.id, customer_phone, {})
        return WebhookReply(to=customer_phone, text="Your cart has been cleared.")
    if verb == "CART":
        return WebhookReply(
            to=customer_phone,
            text=await _build_cart_reply(db, redis, merchant, customer_phone),
        )
    if verb in {"CHECKOUT", "PAY"}:
        return await _handle_checkout(db, redis, merchant, customer_phone)

    return WebhookReply(
        to=customer_phone,
        text=(
            "Unknown command. Reply CATALOG, ADD <SKU> <QTY>, "
            "REMOVE <SKU>, CART, CLEAR, or CHECKOUT."
        ),
    )


async def send_whatsapp_text(
    to_phone: str, body: str, http_client: httpx.AsyncClient | None = None
) -> bool:
    """Send an outbound text reply via Meta's WhatsApp Cloud API if credentials exist."""
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return False

    url = f"https://graph.facebook.com/v21.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": body},
    }
    try:
        if http_client is not None:
            resp = await http_client.post(url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("WhatsApp API transport error for %s: %s", to_phone, exc)
        return False

    if not resp.is_success:
        logger.error("WhatsApp send failed [%s]: %s", resp.status_code, resp.text)
        return False
    return True
