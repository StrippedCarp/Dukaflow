import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from models.merchant import Merchant
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,

)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from core.database import Base

if TYPE_CHECKING:
    from models.merchant import Merchant


class OrderStatus(enum.StrEnum):
    """Lifecycle of an order.

    pending -> paid -> fulfilled
    pending -> failed (M-Pesa payment declined / timed out)
    pending -> cancelled
    """

    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FULFILLED = "fulfilled"


class Order(Base):
    """A customer's order from one merchant, paid via M-Pesa STK Push."""

    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("total_amount >= 0", name="ck_orders_total_non_negative"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    merchant_id: Mapped[int] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            name="order_status",
            native_enum=False,
            create_constraint=True,
            length=20,
            # pyrefly: ignore[implicit-any-lambda]
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=OrderStatus.PENDING,
        server_default=OrderStatus.PENDING.value,
    )
    # Safaricom's ID for the STK Push; used to match the payment callback to this order.
    checkout_request_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    mpesa_receipt_number: Mapped[str | None] = mapped_column(String(50), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    merchant: Mapped["Merchant"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("status", OrderStatus.PENDING)
        super().__init__(**kw)


class OrderItem(Base):
    """One line of an order.

    Name, SKU and price are copied from the product when the order is placed,
    so later price changes or product deletion don't rewrite order history.
    """

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    sku: Mapped[str] = mapped_column(String(50), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity
   
