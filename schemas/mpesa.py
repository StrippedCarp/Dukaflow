from pydantic import BaseModel, Field

from models.order import OrderStatus


class StkMetadataItem(BaseModel):
    """Single key-value item inside Daraja's CallbackMetadata.Item list."""

    name: str = Field(alias="Name")
    value: str | int | float | None = Field(default=None, alias="Value")


class StkCallbackMetadata(BaseModel):
    """Container for CallbackMetadata in successful STK Push callbacks."""

    items: list[StkMetadataItem] = Field(default_factory=list, alias="Item")


class StkCallback(BaseModel):
    """Core stkCallback object posted by Safaricom Daraja."""

    merchant_request_id: str = Field(default="", alias="MerchantRequestID")
    checkout_request_id: str = Field(alias="CheckoutRequestID")
    result_code: int = Field(alias="ResultCode")
    result_desc: str = Field(default="", alias="ResultDesc")
    callback_metadata: StkCallbackMetadata | None = Field(default=None, alias="CallbackMetadata")

    def metadata_dict(self) -> dict[str, str | int | float | None]:
        """Flatten Daraja's Name/Value list into a lookup dictionary."""
        if self.callback_metadata is None:
            return {}
        return {item.name: item.value for item in self.callback_metadata.items}


class StkCallbackBody(BaseModel):
    """Outer Body wrapper in Daraja's STK callback payload."""

    stk_callback: StkCallback = Field(alias="stkCallback")


class MpesaCallbackPayload(BaseModel):
    """Root JSON payload sent by Safaricom Daraja to the callback URL."""

    body: StkCallbackBody = Field(alias="Body")


class MpesaCallbackResponse(BaseModel):
    """Acknowledgement returned to Safaricom Daraja."""

    ResultCode: int = 0
    ResultDesc: str = "Accepted"
    order_id: int | None = None
    order_status: OrderStatus | None = None
