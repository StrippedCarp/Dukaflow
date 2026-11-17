from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.database import get_db
from core.redis import RedisClient
from core.security import CurrentMerchant
from models.order import Order, OrderStatus
from schemas.order import OrderResponse, OrderSummaryResponse
from services.inventory import release_stock
from services.mpesa import (
    collect_order_product_quantities,
    expire_stale_orders,
    initiate_stk_push,
)
from services.reporting import (
    export_orders_csv,
    format_summary_whatsapp_text,
    get_merchant_order_summary,
)
from services.whatsapp import send_whatsapp_text

router = APIRouter(prefix="/orders", tags=["orders"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("/", response_model=list[OrderResponse])
async def list_orders(
    current_merchant: CurrentMerchant,
    db: DbSession,
    status: OrderStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Order]:
    """List the authenticated merchant's orders, newest first."""
    stmt = (
        select(Order)
        .where(Order.merchant_id == current_merchant.id)
        .options(selectinload(Order.items))
    )
    if status is not None:
        stmt = stmt.where(Order.status == status)
    stmt = stmt.order_by(Order.id.desc()).limit(limit).offset(offset)
    return list((await db.scalars(stmt)).all())


@router.get("/summary", response_model=OrderSummaryResponse)
async def order_summary(
    current_merchant: CurrentMerchant,
    db: DbSession,
    start_date: date | None = None,
    end_date: date | None = None,
) -> OrderSummaryResponse:
    """Return aggregated order counts, revenue, and top SKUs for the merchant."""
    try:
        return await get_merchant_order_summary(db, current_merchant.id, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/summary/notify", response_model=OrderSummaryResponse)
async def notify_order_summary(
    current_merchant: CurrentMerchant,
    db: DbSession,
    background_tasks: BackgroundTasks,
    start_date: date | None = None,
    end_date: date | None = None,
) -> OrderSummaryResponse:
    """Compute a sales summary and queue it to the merchant's WhatsApp number."""
    try:
        summary = await get_merchant_order_summary(db, current_merchant.id, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    background_tasks.add_task(
        send_whatsapp_text,
        current_merchant.phone_number,
        format_summary_whatsapp_text(current_merchant.name, summary),
    )
    return summary


@router.get("/export.csv")
async def export_orders_as_csv(
    current_merchant: CurrentMerchant,
    db: DbSession,
    status: OrderStatus | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> Response:
    """Download filtered merchant orders as a sanitized UTF-8 CSV file."""
    try:
        csv_content = await export_orders_csv(
            db,
            current_merchant.id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="orders.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/expire-stale")
async def expire_stale(
    current_merchant: CurrentMerchant,
    db: DbSession,
    max_age_minutes: Annotated[int, Query(ge=1, le=10080)] = 15,
) -> dict[str, int]:
    """Expire abandoned PENDING orders older than ``max_age_minutes`` and release stock."""
    expired_count = await expire_stale_orders(db, current_merchant.id, max_age_minutes)
    return {"expired_count": expired_count}


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: Annotated[int, Path(ge=1)],
    current_merchant: CurrentMerchant,
    db: DbSession,
) -> Order:
    """Fetch a single order belonging to the authenticated merchant."""
    stmt = (
        select(Order)
        .where(Order.id == order_id, Order.merchant_id == current_merchant.id)
        .options(selectinload(Order.items))
    )
    order = await db.scalar(stmt)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.post("/{order_id}/stk-push", response_model=OrderResponse)
async def trigger_stk_push(
    order_id: Annotated[int, Path(ge=1)],
    current_merchant: CurrentMerchant,
    db: DbSession,
    redis: RedisClient,
) -> Order:
    """Initiate (or retry) an M-Pesa STK Push payment prompt for a pending order."""
    merchant_id = current_merchant.id
    order = await db.scalar(
        select(Order).where(Order.id == order_id, Order.merchant_id == merchant_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot initiate payment for order with status '{order.status}'",
        )
    # Detach order and close the read transaction before calling Daraja.
    db.expunge(order)
    await db.rollback()

    lock_key = f"mpesa:lock:stk:{order.id}"
    if not await redis.set(lock_key, "1", nx=True, ex=30):
        raise HTTPException(status_code=409, detail="Payment initiation already in progress")

    try:
        checkout_id = await initiate_stk_push(redis, order)
        if checkout_id is None:
            raise HTTPException(status_code=502, detail="Failed to initiate M-Pesa STK Push")

        locked_order = await db.scalar(
            select(Order)
            .where(Order.id == order_id, Order.merchant_id == merchant_id)
            .options(selectinload(Order.items))
            .with_for_update()
        )
        if locked_order is None or locked_order.status != OrderStatus.PENDING:
            raise HTTPException(status_code=409, detail="Order is no longer pending")

        locked_order.checkout_request_id = checkout_id
        await db.commit()
        await db.refresh(locked_order, attribute_names=["items", "updated_at"])
        return locked_order
    finally:
        await redis.delete(lock_key)


@router.post("/{order_id}/fulfill", response_model=OrderResponse)
async def fulfill_order(
    order_id: Annotated[int, Path(ge=1)],
    current_merchant: CurrentMerchant,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> Order:
    """Mark a PAID order as FULFILLED and notify the customer on WhatsApp."""
    stmt = (
        select(Order)
        .where(Order.id == order_id, Order.merchant_id == current_merchant.id)
        .options(selectinload(Order.items))
        .with_for_update()
    )
    order = await db.scalar(stmt)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.PAID:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot fulfill order with status '{order.status}'",
        )

    order.status = OrderStatus.FULFILLED
    await db.commit()
    await db.refresh(order, attribute_names=["items", "updated_at"])

    background_tasks.add_task(
        send_whatsapp_text,
        order.customer_phone,
        f"Order #{order.id} has been dispatched/fulfilled! Thank you for ordering.",
    )
    return order


@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: Annotated[int, Path(ge=1)],
    current_merchant: CurrentMerchant,
    db: DbSession,
) -> Order:
    """Cancel a pending order and release its reserved inventory back to stock."""
    stmt = (
        select(Order)
        .where(Order.id == order_id, Order.merchant_id == current_merchant.id)
        .options(selectinload(Order.items))
        .with_for_update()
    )
    order = await db.scalar(stmt)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel order with status '{order.status}'",
        )

    items_to_release = collect_order_product_quantities(order)
    if items_to_release:
        await release_stock(db, current_merchant.id, items_to_release)

    order.status = OrderStatus.CANCELLED
    await db.commit()
    await db.refresh(order, attribute_names=["items", "updated_at"])
    return order
