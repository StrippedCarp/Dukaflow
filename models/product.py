from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base

if TYPE_CHECKING:
    from models.merchant import Merchant


class Product(Base):
    """An item a merchant sells, with its price and stock levels.

    ``reserved_qty`` is stock held for orders that are waiting for M-Pesa
    payment, so two customers can't both buy the last unit.
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("merchant_id", "sku", name="uq_products_merchant_sku"),
        CheckConstraint("unit_price >= 0", name="ck_products_unit_price_non_negative"),
        CheckConstraint("stock_qty >= 0", name="ck_products_stock_non_negative"),
        CheckConstraint("reserved_qty >= 0", name="ck_products_reserved_non_negative"),
        CheckConstraint("reserved_qty <= stock_qty", name="ck_products_reserved_le_stock"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    merchant_id: Mapped[int] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sku: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(
        String(100), nullable=False, default="General", server_default=text("'General'")
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stock_qty: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    reserved_qty: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    merchant: Mapped["Merchant"] = relationship(back_populates="products")

    def __init__(self, **kw: Any) -> None:
        # SQLAlchemy column defaults only run on SQL INSERT; set them in memory too.
        kw.setdefault("category", "General")
        kw.setdefault("stock_qty", 0)
        kw.setdefault("reserved_qty", 0)
        kw.setdefault("is_active", True)
        super().__init__(**kw)

    @property
    def available_qty(self) -> int:
        """Units a new customer can actually order right now."""
        return (self.stock_qty or 0) - (self.reserved_qty or 0)
