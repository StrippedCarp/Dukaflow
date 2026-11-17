from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.security import CurrentMerchant
from models.product import Product
from schemas.product import ProductCreate, ProductResponse, ProductUpdate, StockAdjustment
from services.inventory import InsufficientStockError, ProductNotFoundError, adjust_stock

router = APIRouter(prefix="/products", tags=["products"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

DUPLICATE_SKU_DETAIL = "SKU already exists for this merchant"


@router.post("/", response_model=ProductResponse, status_code=201)
async def create_product(
    payload: ProductCreate,
    current_merchant: CurrentMerchant,
    db: DbSession,
) -> Product:
    """Add a product to the authenticated merchant's catalog."""
    existing = await db.scalar(
        select(Product.id).where(
            Product.merchant_id == current_merchant.id,
            Product.sku == payload.sku,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=DUPLICATE_SKU_DETAIL)

    product = Product(merchant_id=current_merchant.id, **payload.model_dump())
    db.add(product)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=DUPLICATE_SKU_DETAIL) from None
    await db.refresh(product)
    return product


@router.get("/", response_model=list[ProductResponse])
async def list_products(
    current_merchant: CurrentMerchant,
    db: DbSession,
    category: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    active_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Product]:
    """List the authenticated merchant's products with optional filters."""
    stmt = select(Product).where(Product.merchant_id == current_merchant.id)
    if category is not None:
        stmt = stmt.where(Product.category == category)
    if active_only:
        stmt = stmt.where(Product.is_active.is_(True))
    stmt = stmt.order_by(Product.id).limit(limit).offset(offset)
    return list((await db.scalars(stmt)).all())


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: Annotated[int, Path(ge=1)],
    current_merchant: CurrentMerchant,
    db: DbSession,
) -> Product:
    """Fetch a single product owned by the authenticated merchant."""
    product = await db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.merchant_id == current_merchant.id,
        )
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.patch("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: Annotated[int, Path(ge=1)],
    payload: ProductUpdate,
    current_merchant: CurrentMerchant,
    db: DbSession,
) -> Product:
    """Update fields on an existing product owned by the authenticated merchant."""
    stmt = (
        select(Product)
        .where(Product.id == product_id, Product.merchant_id == current_merchant.id)
        .with_for_update()
    )
    product = await db.scalar(stmt)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return product
    if "stock_qty" in updates and updates["stock_qty"] < product.reserved_qty:
        raise HTTPException(status_code=409, detail="Cannot reduce stock below reserved quantity")

    for field_name, value in updates.items():
        setattr(product, field_name, value)

    await db.commit()
    await db.refresh(product)
    return product


@router.post("/{product_id}/stock", response_model=ProductResponse)
async def adjust_product_stock(
    product_id: Annotated[int, Path(ge=1)],
    payload: StockAdjustment,
    current_merchant: CurrentMerchant,
    db: DbSession,
) -> Product:
    """Atomically increment or decrement stock for a product."""
    try:
        product = await adjust_stock(db, current_merchant.id, product_id, payload.delta)
    except ProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except InsufficientStockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await db.commit()
    await db.refresh(product)
    return product
