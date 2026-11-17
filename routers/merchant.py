from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.rate_limit import rate_limit_merchant_registration
from core.security import CurrentMerchant, generate_api_key
from models.merchant import Merchant
from schemas.merchant import MerchantCreate, MerchantCreatedResponse, MerchantResponse

router = APIRouter(prefix="/merchants", tags=["merchants"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

DUPLICATE_DETAIL = "Email or phone number already registered"


@router.post(
    "/",
    response_model=MerchantCreatedResponse,
    status_code=201,
    dependencies=[Depends(rate_limit_merchant_registration)],
)
async def create_merchant(payload: MerchantCreate, db: DbSession) -> MerchantCreatedResponse:
    """Register a new merchant and issue a one-time API key. Returns 409 on conflict."""
    existing = await db.scalar(
        select(Merchant.id).where(
            or_(Merchant.email == payload.email, Merchant.phone_number == payload.phone_number)
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=DUPLICATE_DETAIL)

    raw_key, key_hash = generate_api_key()
    merchant = Merchant(
        name=payload.name,
        email=payload.email,
        phone_number=payload.phone_number,
        api_key_hash=key_hash,
    )
    db.add(merchant)
    try:
        await db.commit()
    except IntegrityError:
        # Two requests raced past the check above; the DB unique constraint caught it.
        await db.rollback()
        raise HTTPException(status_code=409, detail=DUPLICATE_DETAIL) from None
    await db.refresh(merchant)
    return MerchantCreatedResponse(
        id=merchant.id,
        name=merchant.name,
        email=merchant.email,
        phone_number=merchant.phone_number,
        created_at=merchant.created_at,
        api_key=raw_key,
    )


@router.get("/me", response_model=MerchantResponse)
async def get_my_merchant(current_merchant: CurrentMerchant) -> Merchant:
    """Return the authenticated merchant's own profile."""
    return current_merchant


@router.get("/{merchant_id}", response_model=MerchantResponse)
async def get_merchant(
    merchant_id: Annotated[int, Path(ge=1)],
    current_merchant: CurrentMerchant,
) -> Merchant:
    """Fetch a merchant profile by ID (must match the authenticated merchant)."""
    if merchant_id != current_merchant.id:
        raise HTTPException(status_code=403, detail="Cannot access another merchant's profile")
    return current_merchant
