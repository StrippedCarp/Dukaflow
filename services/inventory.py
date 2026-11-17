"""Atomic inventory operations.

Every operation locks the target product rows with ``SELECT ... FOR UPDATE``
in ascending ``Product.id`` order. Consistent lock ordering prevents deadlocks
when two orders compete for overlapping products at the same time.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.product import Product


class InventoryError(Exception):
    """Base error for stock and catalog operations."""


class ProductNotFoundError(InventoryError):
    """Raised when a requested product is missing, inactive, or owned by another merchant."""


class InsufficientStockError(InventoryError):
    """Raised when available or reserved stock is too low for the requested change."""


async def _lock_products(
    db: AsyncSession, merchant_id: int, items: dict[int, int]
) -> list[Product]:
    """Validate positive quantities and row-lock all requested products in deterministic order."""
    if not items or any(qty <= 0 for qty in items.values()):
        raise ValueError("Each item quantity must be positive")

    stmt = (
        select(Product)
        .where(Product.merchant_id == merchant_id, Product.id.in_(sorted(items)))
        .order_by(Product.id)
        .with_for_update()
    )
    products = list((await db.scalars(stmt)).all())
    if len(products) != len(items):
        raise ProductNotFoundError("One or more products not found")
    return products


async def adjust_stock(db: AsyncSession, merchant_id: int, product_id: int, delta: int) -> Product:
    """Increase or decrease a product's physical stock and flush the change."""
    stmt = (
        select(Product)
        .where(Product.id == product_id, Product.merchant_id == merchant_id)
        .with_for_update()
    )
    product = await db.scalar(stmt)
    if product is None:
        raise ProductNotFoundError("Product not found")

    new_stock = product.stock_qty + delta
    if new_stock < 0:
        raise InsufficientStockError("Stock quantity cannot be negative")
    if new_stock < product.reserved_qty:
        raise InsufficientStockError("Cannot reduce stock below reserved quantity")

    product.stock_qty = new_stock
    await db.flush()
    return product


async def reserve_stock(db: AsyncSession, merchant_id: int, items: dict[int, int]) -> list[Product]:
    """Hold ``items`` ({product_id: qty}) for a pending order awaiting payment.

    Flushes changes to the open transaction without committing so the caller can
    create the Order row atomically in the same transaction.
    """
    products = await _lock_products(db, merchant_id, items)
    if any(not p.is_active for p in products):
        raise ProductNotFoundError("One or more products are unavailable")

    for p in products:
        if p.available_qty < items[p.id]:
            raise InsufficientStockError(f"Insufficient stock for {p.sku}")

    for p in products:
        p.reserved_qty += items[p.id]

    await db.flush()
    return products


async def release_stock(db: AsyncSession, merchant_id: int, items: dict[int, int]) -> None:
    """Release reserved stock back to available inventory (e.g. failed or expired order)."""
    products = await _lock_products(db, merchant_id, items)
    for p in products:
        if p.reserved_qty < items[p.id]:
            raise InsufficientStockError(f"Cannot release more than reserved for {p.sku}")

    for p in products:
        p.reserved_qty -= items[p.id]

    await db.flush()


async def commit_stock(db: AsyncSession, merchant_id: int, items: dict[int, int]) -> None:
    """Convert reserved stock into sold stock once M-Pesa payment is confirmed."""
    products = await _lock_products(db, merchant_id, items)
    for p in products:
        qty = items[p.id]
        if p.reserved_qty < qty or p.stock_qty < qty:
            raise InsufficientStockError(f"Cannot commit unreserved stock for {p.sku}")

    for p in products:
        qty = items[p.id]
        p.reserved_qty -= qty
        p.stock_qty -= qty

    await db.flush()
