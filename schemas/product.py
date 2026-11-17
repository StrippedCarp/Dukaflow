from decimal import Decimal
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

SkuStr = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=50,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9\-_.]*$",
    ),
    AfterValidator(str.upper),
]
ProductName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=255)]
CategoryName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)
]
MoneyAmount = Annotated[Decimal, Field(ge=Decimal("0.00"), max_digits=12, decimal_places=2)]
StockQty = Annotated[int, Field(ge=0, le=1_000_000_000)]


def _non_zero(value: int) -> int:
    if value == 0:
        raise ValueError("delta must not be zero")
    return value


class ProductCreate(BaseModel):
    """Body for POST /products/."""

    sku: SkuStr
    name: ProductName
    category: CategoryName = "General"
    unit_price: MoneyAmount
    stock_qty: StockQty = 0
    is_active: bool = True


class ProductUpdate(BaseModel):
    """Body for PATCH /products/{id}. Only supplied fields are updated."""

    model_config = ConfigDict(extra="forbid")

    name: ProductName | None = None
    category: CategoryName | None = None
    unit_price: MoneyAmount | None = None
    stock_qty: StockQty | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> "ProductUpdate":
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class StockAdjustment(BaseModel):
    """Body for POST /products/{id}/stock."""

    delta: Annotated[int, Field(gt=-1_000_000, lt=1_000_000), AfterValidator(_non_zero)]


class ProductResponse(BaseModel):
    """API representation of a catalog item."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    merchant_id: int
    sku: str
    name: str
    category: str
    unit_price: Decimal
    stock_qty: int
    reserved_qty: int
    available_qty: int
    is_active: bool
