from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from models.order import OrderStatus


class OrderItemResponse(BaseModel):
    """One line item in an order response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int | None
    sku: str
    product_name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class OrderResponse(BaseModel):
    """Order details including snapshotted line items."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    merchant_id: int
    customer_phone: str
    total_amount: Decimal
    status: OrderStatus
    checkout_request_id: str | None
    mpesa_receipt_number: str | None = None
    created_at: datetime
    items: list[OrderItemResponse]


class TopProductItem(BaseModel):
    """Aggregated sales for a single SKU across settled (paid/fulfilled) orders."""

    sku: str
    product_name: str
    quantity_sold: int
    revenue: Decimal


class OrderSummaryResponse(BaseModel):
    """Aggregated order metrics for a merchant over an optional date window."""

    total_orders: int
    paid_orders: int
    fulfilled_orders: int
    pending_orders: int
    failed_orders: int
    cancelled_orders: int
    gross_revenue: Decimal
    pending_value: Decimal
    top_products: list[TopProductItem]
