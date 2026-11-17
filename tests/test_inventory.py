from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models import Merchant, Product
from services.inventory import (
    InsufficientStockError,
    ProductNotFoundError,
    adjust_stock,
    commit_stock,
    release_stock,
    reserve_stock,
)


async def seed_catalog(db: AsyncSession) -> tuple[Merchant, Merchant, Product, Product, Product]:
    m1 = Merchant(name="M1", email="m1@example.com", phone_number="254712345671")
    m2 = Merchant(name="M2", email="m2@example.com", phone_number="254712345672")
    db.add_all([m1, m2])
    await db.flush()

    p1 = Product(
        merchant_id=m1.id, sku="OIL-1L", name="Oil 1L", unit_price=Decimal("320"), stock_qty=10
    )
    p2 = Product(
        merchant_id=m1.id, sku="SUGAR-2KG", name="Sugar 2KG", unit_price=Decimal("280"), stock_qty=5
    )
    inactive = Product(
        merchant_id=m1.id,
        sku="OLD-ITEM",
        name="Discontinued",
        unit_price=Decimal("100"),
        stock_qty=10,
        is_active=False,
    )
    db.add_all([p1, p2, inactive])
    await db.commit()
    return m1, m2, p1, p2, inactive


async def test_reserve_then_commit_stock(db: AsyncSession) -> None:
    m1, _, p1, p2, _ = await seed_catalog(db)

    reserved = await reserve_stock(db, m1.id, {p2.id: 2, p1.id: 4})
    await db.commit()
    assert [p.id for p in reserved] == sorted([p1.id, p2.id])
    assert p1.reserved_qty == 4
    assert p1.available_qty == 6
    assert p2.reserved_qty == 2
    assert p2.available_qty == 3

    await commit_stock(db, m1.id, {p1.id: 4, p2.id: 2})
    await db.commit()
    assert p1.stock_qty == 6
    assert p1.reserved_qty == 0
    assert p2.stock_qty == 3
    assert p2.reserved_qty == 0


async def test_reserve_then_release_stock(db: AsyncSession) -> None:
    m1, _, p1, _, _ = await seed_catalog(db)

    await reserve_stock(db, m1.id, {p1.id: 3})
    await db.commit()
    assert p1.available_qty == 7

    # Product gets deactivated while order is pending; release must still work.
    p1.is_active = False
    await release_stock(db, m1.id, {p1.id: 3})
    await db.commit()
    assert p1.stock_qty == 10
    assert p1.reserved_qty == 0
    assert p1.available_qty == 10


async def test_commit_stock_allows_deactivated_product(db: AsyncSession) -> None:
    m1, _, p1, _, _ = await seed_catalog(db)

    await reserve_stock(db, m1.id, {p1.id: 3})
    await db.commit()

    # Merchant marks product inactive while payment is in-flight; commit must still finish.
    p1.is_active = False
    await commit_stock(db, m1.id, {p1.id: 3})
    await db.commit()
    assert p1.stock_qty == 7
    assert p1.reserved_qty == 0


async def test_reserve_stock_partial_failure_leaves_no_mutations(db: AsyncSession) -> None:
    m1, _, p1, p2, _ = await seed_catalog(db)

    with pytest.raises(InsufficientStockError):
        await reserve_stock(db, m1.id, {p1.id: 2, p2.id: 99})
    await db.rollback()
    await db.refresh(p1)
    assert p1.reserved_qty == 0


async def test_reserve_stock_errors(db: AsyncSession) -> None:
    m1, m2, p1, _, inactive = await seed_catalog(db)

    with pytest.raises(InsufficientStockError):
        await reserve_stock(db, m1.id, {p1.id: 11})

    with pytest.raises(ProductNotFoundError):
        await reserve_stock(db, m1.id, {inactive.id: 1})

    with pytest.raises(ProductNotFoundError):
        await reserve_stock(db, m2.id, {p1.id: 1})  # wrong merchant


async def test_release_and_commit_stock_errors(db: AsyncSession) -> None:
    m1, m2, p1, _, _ = await seed_catalog(db)
    await reserve_stock(db, m1.id, {p1.id: 2})
    await db.commit()

    with pytest.raises(InsufficientStockError):
        await release_stock(db, m1.id, {p1.id: 3})

    with pytest.raises(InsufficientStockError):
        await commit_stock(db, m1.id, {p1.id: 3})

    with pytest.raises(ProductNotFoundError):
        await release_stock(db, m2.id, {p1.id: 1})

    with pytest.raises(ProductNotFoundError):
        await commit_stock(db, m2.id, {p1.id: 1})


@pytest.mark.parametrize("bad_items", [{}, {1: 0}, {1: -2}])
async def test_inventory_operations_reject_invalid_quantities(
    db: AsyncSession, bad_items: dict[int, int]
) -> None:
    with pytest.raises(ValueError, match="positive"):
        await reserve_stock(db, 1, bad_items)
    with pytest.raises(ValueError, match="positive"):
        await release_stock(db, 1, bad_items)
    with pytest.raises(ValueError, match="positive"):
        await commit_stock(db, 1, bad_items)


async def test_adjust_stock_service(db: AsyncSession) -> None:
    m1, m2, p1, _, _ = await seed_catalog(db)
    await reserve_stock(db, m1.id, {p1.id: 4})
    await db.commit()

    updated = await adjust_stock(db, m1.id, p1.id, 5)
    await db.commit()
    assert updated.stock_qty == 15

    with pytest.raises(InsufficientStockError, match="negative"):
        await adjust_stock(db, m1.id, p1.id, -16)

    with pytest.raises(InsufficientStockError, match="reserved"):
        await adjust_stock(db, m1.id, p1.id, -12)  # would drop to 3 < reserved 4

    with pytest.raises(ProductNotFoundError):
        await adjust_stock(db, m2.id, p1.id, 1)
