"""Database-level rules. These protect data even if application code has a bug."""

from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Merchant, Order, OrderItem, OrderStatus, Product


async def make_merchant(db: AsyncSession, n: int = 0) -> Merchant:
    merchant = Merchant(
        name=f"Merchant {n}", email=f"m{n}@example.com", phone_number=f"25471234567{n}"
    )
    db.add(merchant)
    await db.commit()
    return merchant


async def make_product(db: AsyncSession, merchant: Merchant, **kw: object) -> Product:
    fields: dict[str, object] = {
        "sku": "OIL-1L",
        "name": "Cooking oil 1L",
        "unit_price": Decimal("320.00"),
    }
    fields.update(kw)
    product = Product(merchant_id=merchant.id, **fields)
    db.add(product)
    await db.commit()
    return product


async def test_product_in_memory_defaults() -> None:
    product = Product(merchant_id=1, sku="SKU-1", name="Test", unit_price=Decimal("100.00"))
    assert product.available_qty == 0
    assert product.category == "General"
    assert product.is_active is True


async def test_product_defaults_and_available_qty(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    product = await make_product(db, merchant, stock_qty=10, reserved_qty=3)
    await db.refresh(product)
    assert product.category == "General"
    assert product.is_active is True
    assert product.available_qty == 7


@pytest.mark.parametrize(
    "fields",
    [
        {"stock_qty": 5, "reserved_qty": 6},  # can't reserve more than you have
        {"stock_qty": -1},
        {"reserved_qty": -1},
        {"unit_price": Decimal("-0.01")},
    ],
)
async def test_product_check_constraints(db: AsyncSession, fields: dict[str, object]) -> None:
    merchant = await make_merchant(db)
    with pytest.raises(IntegrityError):
        await make_product(db, merchant, **fields)


async def test_sku_unique_per_merchant_only(db: AsyncSession) -> None:
    m1 = await make_merchant(db, 1)
    m2 = await make_merchant(db, 2)
    await make_product(db, m1)
    await make_product(db, m2)  # same SKU, different merchant: allowed
    with pytest.raises(IntegrityError):
        await make_product(db, m1)


async def test_order_in_memory_default_status() -> None:
    order = Order(merchant_id=1, customer_phone="254700000001", total_amount=Decimal("100.00"))
    assert order.status is OrderStatus.PENDING


async def test_order_defaults_to_pending_and_snapshots_items(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    product = await make_product(db, merchant, stock_qty=10)
    order = Order(
        merchant_id=merchant.id,
        customer_phone="254700000001",
        total_amount=Decimal("640.00"),
        items=[
            OrderItem(
                product_id=product.id,
                sku=product.sku,
                product_name=product.name,
                quantity=2,
                unit_price=product.unit_price,
            )
        ],
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    assert order.status is OrderStatus.PENDING
    item = (await db.scalars(select(OrderItem))).one()
    assert item.line_total == Decimal("640.00")


async def test_db_rejects_unknown_order_status(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO orders (merchant_id, customer_phone, total_amount, status) "
                "VALUES (:m, '254700000001', 10, 'teleported')"
            ),
            {"m": merchant.id},
        )


async def test_order_negative_total_rejected(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    db.add(
        Order(merchant_id=merchant.id, customer_phone="254700000001", total_amount=Decimal("-1"))
    )
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_order_item_quantity_must_be_positive(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    order = Order(
        merchant_id=merchant.id,
        customer_phone="254700000001",
        total_amount=Decimal("0"),
        items=[OrderItem(sku="X", product_name="X", quantity=0, unit_price=Decimal("1"))],
    )
    db.add(order)
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_order_item_negative_unit_price_rejected(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    order = Order(
        merchant_id=merchant.id,
        customer_phone="254700000001",
        total_amount=Decimal("0"),
        items=[OrderItem(sku="X", product_name="X", quantity=1, unit_price=Decimal("-0.01"))],
    )
    db.add(order)
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_checkout_request_id_is_unique(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    for _ in range(2):
        db.add(
            Order(
                merchant_id=merchant.id,
                customer_phone="254700000001",
                total_amount=Decimal("1"),
                checkout_request_id="ws_CO_123",
            )
        )
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_deleting_product_keeps_order_history(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    product = await make_product(db, merchant)
    order = Order(
        merchant_id=merchant.id,
        customer_phone="254700000001",
        total_amount=Decimal("320.00"),
        items=[
            OrderItem(
                product_id=product.id,
                sku="OIL-1L",
                product_name="Cooking oil 1L",
                quantity=1,
                unit_price=Decimal("320.00"),
            )
        ],
    )
    db.add(order)
    await db.commit()

    await db.execute(text("DELETE FROM products WHERE id = :id"), {"id": product.id})
    await db.commit()

    row = (await db.execute(text("SELECT product_id, product_name FROM order_items"))).one()
    assert row.product_id is None
    assert row.product_name == "Cooking oil 1L"


async def test_deleting_order_cascades_to_items(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    order = Order(
        merchant_id=merchant.id,
        customer_phone="254700000001",
        total_amount=Decimal("100.00"),
        items=[OrderItem(sku="X", product_name="Item X", quantity=1, unit_price=Decimal("100.00"))],
    )
    db.add(order)
    await db.commit()

    await db.delete(order)
    await db.commit()

    assert await db.scalar(text("SELECT count(*) FROM order_items")) == 0


async def test_deleting_merchant_cascades(db: AsyncSession) -> None:
    merchant = await make_merchant(db)
    product = await make_product(db, merchant)
    db.add(
        Order(
            merchant_id=merchant.id,
            customer_phone="254700000001",
            total_amount=Decimal("10.00"),
            items=[
                OrderItem(
                    product_id=product.id,
                    sku=product.sku,
                    product_name=product.name,
                    quantity=1,
                    unit_price=Decimal("10.00"),
                )
            ],
        )
    )
    await db.commit()

    # Delete via ORM (not raw SQL) to verify passive_deletes works with non-null FKs.
    await db.delete(merchant)
    await db.commit()

    for table in ("products", "orders", "order_items"):
        count = await db.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608 - fixed names
        assert count == 0
