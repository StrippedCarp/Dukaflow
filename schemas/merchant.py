from sqlalchemy.engine.processors import str_to_date
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, StringConstraints

from core.phone import normalize_kenyan_phone

KenyanPhone = Annotated[str, AfterValidator(normalize_kenyan_phone)]
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]
MerchantName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=2, max_length=255)
]


class MerchantCreate(BaseModel):
    """Body for POST /merchants/. Phone is normalised to 2547XXXXXXXX."""

    name: MerchantName
    email: NormalizedEmail
    phone_number: KenyanPhone


class MerchantResponse(BaseModel):
    """Response schema for a merchant"""

    id : int
    name : str
    email : EmailStr
    phone_number : str
    created_at : datetime

    class MerchantCreatedResponse(MerchantResponse):
        """Returns once at sign-up; includes the raw API key."""

        api_key: str