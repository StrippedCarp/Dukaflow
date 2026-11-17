import hashlib
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base

if TYPE_CHECKING:
    from models.order import Order
    from models.product import Product


class Merchant(Base):
    """A business (e.g. a wholesaler) that sells products through DukaFlow."""

    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    orders: Mapped[list["Order"]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan", passive_deletes=True
    )
    products: Mapped[list["Product"]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan", passive_deletes=True
    )

    def __init__(self, **kw: Any) -> None:
        if "api_key_hash" not in kw:
            kw["api_key_hash"] = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        super().__init__(**kw)
