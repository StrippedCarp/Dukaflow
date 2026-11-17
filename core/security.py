"""Merchant API key generation, hashing, and FastAPI authentication dependency.

Raw API keys (e.g. ``dfk_<random>``) are shown to the merchant once at sign-up.
Only their SHA-256 hex digest is stored in the database, so a database leak
never exposes usable credentials.
"""

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from models.merchant import Merchant

API_KEY_PREFIX = "dfk_"
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
def generate_api_key() -> tuple[str, str]:
    raw_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return raw_key, hash_api_key(raw_key)


async def get_current_merchant(
    api_key: Annotated[str | None, Depends(_api_key_header)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Merchant:
    """Authenticate a request via the ``X-API-Key`` header."""
    cleaned_key = api_key.strip() if api_key else ""
    if not cleaned_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    key_hash = hash_api_key(cleaned_key)
    merchant = await db.scalar(select(Merchant).where(Merchant.api_key_hash == key_hash))
    if merchant is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return merchant


CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]
