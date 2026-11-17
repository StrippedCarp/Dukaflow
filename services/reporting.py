"""Merchant reporting: sales summaries, WhatsApp digests, and sanitized CSV exports."""

import csv
import io
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.order import Order, OrderItem, OrderStatus
from schemas.order import OrderSummaryResponse, TopProductItem

# Kenya observes East Africa Time (UTC+3) year-round with no daylight saving time.
EAT = timezone(timedelta(hours=3), name="EAT")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _date_bounds(
    start_date: date | None, end_date: date | None
) -> tuple[datetime | None, datetime | None]:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date cannot be after end_date")
    start_dt = (
        datetime.combine(start_date, time.min, tzinfo=EAT).astimezone(UTC) if start_date else None
    )
    end_dt = datetime.combine(end_date, time.max, tzinfo=EAT).astimezone(UTC) if end_date else None
    return start_dt, end_dt


def _order_conditions(
    merchant_id: int,
    *,
    status: OrderStatus | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[ColumnElement[bool]]:
    start_dt, end_dt = _date_bounds(start_date, end_date)
    conditions: list[ColumnElement[bool]] = [Order.merchant_id == merchant_id]
    if status is not None:
        conditions.append(Order.status == status)
    if start_dt is not None:
        conditions.append(Order.created_at >= start_dt)
    if end_dt is not None:
        conditions.append(Order.created_at <= end_dt)
    return conditions


async def get_merchant_order_summary(
    db: AsyncSession,
    merchant_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
) -> OrderSummaryResponse:
    """Compute order counts, revenue totals, and top SKUs for ``merchant_id``."""
    conditions = _order_conditions(merchant_id, start_date=start_date, end_date=end_date)

    status_stmt = (
        select(
            Order.status,
            func.count(Order.id),
            func.coalesce(func.sum(Order.total_amount), Decimal("0.00")),
        )
        .where(*conditions)
        .group_by(Order.status)
    )
    status_rows = (await db.execute(status_stmt)).all()

    counts: dict[OrderStatus, int] = dict.fromkeys(OrderStatus, 0)
    amounts: dict[OrderStatus, Decimal] = dict.fromkeys(OrderStatus, Decimal("0.00"))
    for row_status, row_count, row_total in status_rows:
        counts[row_status] = int(row_count)
        amounts[row_status] = Decimal(row_total)

    gross_revenue = amounts[OrderStatus.PAID] + amounts[OrderStatus.FULFILLED]
    pending_value = amounts[OrderStatus.PENDING]

    rev_expr = func.sum(OrderItem.unit_price * OrderItem.quantity)
    top_stmt = (
        select(
            OrderItem.sku,
            func.max(OrderItem.product_name),
            func.sum(OrderItem.quantity),
            rev_expr,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(*conditions, Order.status.in_((OrderStatus.PAID, OrderStatus.FULFILLED)))
        .group_by(OrderItem.sku)
        .order_by(rev_expr.desc(), OrderItem.sku.asc())
        .limit(10)
    )
    top_rows = (await db.execute(top_stmt)).all()
    top_products = [
        TopProductItem(
            sku=sku,
            product_name=str(name),
            quantity_sold=int(qty),
            revenue=Decimal(rev),
        )
        for sku, name, qty, rev in top_rows
    ]

    return OrderSummaryResponse(
        total_orders=sum(counts.values()),
        paid_orders=counts[OrderStatus.PAID],
        fulfilled_orders=counts[OrderStatus.FULFILLED],
        pending_orders=counts[OrderStatus.PENDING],
        failed_orders=counts[OrderStatus.FAILED],
        cancelled_orders=counts[OrderStatus.CANCELLED],
        gross_revenue=gross_revenue,
        pending_value=pending_value,
        top_products=top_products,
    )


def format_summary_whatsapp_text(merchant_name: str, summary: OrderSummaryResponse) -> str:
    """Format a merchant's sales summary into a readable WhatsApp digest."""
    lines = [
        f"{merchant_name} Sales Summary:",
        f"- Settled Orders: {summary.paid_orders + summary.fulfilled_orders} "
        f"(Paid: {summary.paid_orders}, Fulfilled: {summary.fulfilled_orders})",
        f"- Gross Revenue: KES {summary.gross_revenue:.2f}",
        f"- Pending Orders: {summary.pending_orders} (KES {summary.pending_value:.2f})",
        f"- Failed/Cancelled: {summary.failed_orders + summary.cancelled_orders}",
    ]
    if summary.top_products:
        lines.append("Top Products:")
        for tp in summary.top_products[:5]:
            lines.append(f"  * {tp.sku}: {tp.quantity_sold} units (KES {tp.revenue:.2f})")
    return "\n".join(lines)


def sanitize_csv_cell(value: object) -> str:
    """Prefix leading formula characters so spreadsheet apps never evaluate text cells."""
    if value is None:
        return ""
    cell = str(value)
    stripped = cell.lstrip()
    if (cell and cell[0] in _CSV_FORMULA_PREFIXES) or (
        stripped and stripped[0] in _CSV_FORMULA_PREFIXES
    ):
        return f"'{cell}"
    return cell


async def export_orders_csv(
    db: AsyncSession,
    merchant_id: int,
    *,
    status: OrderStatus | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 1000,
) -> str:
    """Export filtered orders for ``merchant_id`` as a formula-safe UTF-8 CSV string."""
    conditions = _order_conditions(
        merchant_id, status=status, start_date=start_date, end_date=end_date
    )
    stmt = (
        select(Order)
        .where(*conditions)
        .options(selectinload(Order.items))
        .order_by(Order.id.desc())
        .limit(limit)
    )
    orders = list((await db.scalars(stmt)).all())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "order_id",
            "created_at",
            "customer_phone",
            "status",
            "total_amount",
            "mpesa_receipt_number",
            "checkout_request_id",
            "items_summary",
        ]
    )
    for order in orders:
        items_summary = "; ".join(f"{item.sku}x{item.quantity}" for item in order.items)
        writer.writerow(
            [
                order.id,
                order.created_at.isoformat(),
                sanitize_csv_cell(order.customer_phone),
                order.status.value,
                f"{order.total_amount:.2f}",
                sanitize_csv_cell(order.mpesa_receipt_number),
                sanitize_csv_cell(order.checkout_request_id),
                sanitize_csv_cell(items_summary),
            ]
        )
    return buf.getvalue()
