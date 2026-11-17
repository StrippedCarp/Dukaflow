from typing import Any

import pytest
from httpx import AsyncClient


def merchant_payload(**overrides: Any) -> dict[str, Any]:
    data = {
        "name": "Mama Mboga Wholesalers",
        "email": "mama@example.com",
        "phone_number": "0712345678",
    }
    data.update(overrides)
    return data


async def test_create_merchant_normalises_input_and_returns_api_key(client: AsyncClient) -> None:
    resp = await client.post(
        "/merchants/",
        json=merchant_payload(name="  Mama Mboga  ", email="Mama@Example.COM"),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["name"] == "Mama Mboga"
    assert body["email"] == "mama@example.com"
    assert body["phone_number"] == "254712345678"
    assert body["api_key"].startswith("dfk_")
    assert len(body["api_key"]) > 20
    assert "created_at" in body


async def test_duplicate_email_case_insensitive_conflicts(client: AsyncClient) -> None:
    assert (await client.post("/merchants/", json=merchant_payload())).status_code == 201
    resp = await client.post(
        "/merchants/", json=merchant_payload(email="MAMA@example.com", phone_number="0722000000")
    )
    assert resp.status_code == 409


async def test_duplicate_phone_in_different_format_conflicts(client: AsyncClient) -> None:
    assert (await client.post("/merchants/", json=merchant_payload())).status_code == 201
    resp = await client.post(
        "/merchants/",
        json=merchant_payload(email="other@example.com", phone_number="+254712345678"),
    )
    assert resp.status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"phone_number": "12345"},
        {"email": "not-an-email"},
        {"name": " a "},
        {"name": "x" * 256},
    ],
)
async def test_invalid_input_rejected(client: AsyncClient, overrides: dict[str, Any]) -> None:
    resp = await client.post("/merchants/", json=merchant_payload(**overrides))
    assert resp.status_code == 422


async def test_get_my_merchant_with_valid_api_key(client: AsyncClient) -> None:
    created = (await client.post("/merchants/", json=merchant_payload())).json()
    api_key = created.pop("api_key")

    resp = await client.get("/merchants/me", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json() == created
    assert "api_key" not in resp.json()
    assert "api_key_hash" not in resp.json()


async def test_get_my_merchant_rejects_missing_or_invalid_key(client: AsyncClient) -> None:
    assert (await client.get("/merchants/me")).status_code == 401
    assert (
        await client.get("/merchants/me", headers={"X-API-Key": "dfk_wrong"})
    ).status_code == 401


async def test_get_merchant_by_id_requires_owner(client: AsyncClient) -> None:
    m1 = (await client.post("/merchants/", json=merchant_payload())).json()
    m2 = (
        await client.post(
            "/merchants/",
            json=merchant_payload(email="other@example.com", phone_number="0722000000"),
        )
    ).json()

    own = await client.get(f"/merchants/{m1['id']}", headers={"X-API-Key": m1["api_key"]})
    assert own.status_code == 200
    assert own.json()["id"] == m1["id"]

    other = await client.get(f"/merchants/{m2['id']}", headers={"X-API-Key": m1["api_key"]})
    assert other.status_code == 403


async def test_get_invalid_merchant_id_422(client: AsyncClient) -> None:
    created = (await client.post("/merchants/", json=merchant_payload())).json()
    resp = await client.get("/merchants/0", headers={"X-API-Key": created["api_key"]})
    assert resp.status_code == 422


async def test_concurrent_duplicate_signup_returns_409(client: AsyncClient) -> None:
    """Simulate two requests racing past the duplicate check at the same moment.

    The pre-check is bypassed, so only the database's UNIQUE constraint can
    stop the duplicate. The API must still answer 409, not crash with a 500.
    """
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from core.database import engine, get_db
    from main import app

    class RacingSession(AsyncSession):
        async def scalar(self, *args: Any, **kwargs: Any) -> Any:
            return None  # pretend the pre-check found nothing

    async def racing_db() -> AsyncIterator[AsyncSession]:
        async with RacingSession(engine, expire_on_commit=False) as session:
            yield session

    assert (await client.post("/merchants/", json=merchant_payload())).status_code == 201
    app.dependency_overrides[get_db] = racing_db
    try:
        resp = await client.post("/merchants/", json=merchant_payload())
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Email or phone number already registered"
