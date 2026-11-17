from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Product


async def create_merchant_and_headers(
    client: AsyncClient, idx: int = 0
) -> tuple[int, dict[str, str]]:
    resp = await client.post(
        "/merchants/",
        json={
            "name": f"Wholesaler {idx}",
            "email": f"w{idx}@example.com",
            "phone_number": f"071234567{idx}",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    return body["id"], {"X-API-Key": body["api_key"]}


def product_payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "sku": "oil-1l",
        "name": "Fresh Fri Cooking Oil 1L",
        "unit_price": "320.00",
        "stock_qty": 50,
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/products/"),
        ("POST", "/products/"),
        ("GET", "/products/1"),
        ("PATCH", "/products/1"),
        ("POST", "/products/1/stock"),
        ("GET", "/merchants/1"),
    ],
)
async def test_routes_reject_missing_whitespace_or_invalid_auth(
    client: AsyncClient, method: str, path: str
) -> None:
    res_missing = await client.request(method, path)
    assert res_missing.status_code == 401
    assert res_missing.headers.get("www-authenticate") == "ApiKey"

    res_blank = await client.request(method, path, headers={"X-API-Key": "   "})
    assert res_blank.status_code == 401

    res_invalid = await client.request(method, path, headers={"X-API-Key": "dfk_wrong"})
    assert res_invalid.status_code == 401
    assert res_invalid.headers.get("www-authenticate") == "ApiKey"


async def test_api_key_header_trims_surrounding_whitespace(client: AsyncClient) -> None:
    _, headers = await create_merchant_and_headers(client)
    resp = await client.get("/products/", headers={"X-API-Key": f"  {headers['X-API-Key']}  "})
    assert resp.status_code == 200


async def test_create_product_normalises_sku_and_sets_defaults(client: AsyncClient) -> None:
    merchant_id, headers = await create_merchant_and_headers(client)
    resp = await client.post(
        "/products/",
        headers=headers,
        json={"sku": "  oil-1l  ", "name": "  Cooking Oil 1L  ", "unit_price": "320.50"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["merchant_id"] == merchant_id
    assert body["sku"] == "OIL-1L"
    assert body["name"] == "Cooking Oil 1L"
    assert body["category"] == "General"
    assert Decimal(body["unit_price"]) == Decimal("320.50")
    assert body["stock_qty"] == 0
    assert body["reserved_qty"] == 0
    assert body["available_qty"] == 0
    assert body["is_active"] is True


async def test_duplicate_sku_conflicts_per_merchant_only(client: AsyncClient) -> None:
    _, h1 = await create_merchant_and_headers(client, 1)
    _, h2 = await create_merchant_and_headers(client, 2)

    assert (await client.post("/products/", headers=h1, json=product_payload())).status_code == 201
    # Same merchant, uppercase variant -> 409
    dup = await client.post("/products/", headers=h1, json=product_payload(sku="OIL-1L"))
    assert dup.status_code == 409
    # Different merchant, same SKU -> 201
    other = await client.post("/products/", headers=h2, json=product_payload())
    assert other.status_code == 201


async def test_concurrent_duplicate_sku_returns_409(client: AsyncClient) -> None:
    from core.database import engine, get_db
    from main import app

    _, headers = await create_merchant_and_headers(client)
    assert (
        await client.post("/products/", headers=headers, json=product_payload())
    ).status_code == 201

    class AuthAwareRacingSession(AsyncSession):
        async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            # Let merchant authentication succeed; pretend SKU pre-check found no row.
            if "merchants" in str(statement).lower():
                return await super().scalar(statement, *args, **kwargs)
            return None

    async def auth_racing_db() -> AsyncIterator[AsyncSession]:
        async with AuthAwareRacingSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_db] = auth_racing_db
    try:
        resp = await client.post("/products/", headers=headers, json=product_payload())
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"sku": "-bad"},
        {"sku": "has spaces"},
        {"name": "A"},
        {"category": " "},
        {"unit_price": "-1.00"},
        {"unit_price": "10.999"},
        {"stock_qty": -5},
        {"stock_qty": 1_000_000_001},
    ],
)
async def test_invalid_product_input_rejected(
    client: AsyncClient, overrides: dict[str, Any]
) -> None:
    _, headers = await create_merchant_and_headers(client)
    resp = await client.post("/products/", headers=headers, json=product_payload(**overrides))
    assert resp.status_code == 422


async def test_create_product_allows_zero_unit_price(client: AsyncClient) -> None:
    _, headers = await create_merchant_and_headers(client)
    resp = await client.post(
        "/products/", headers=headers, json=product_payload(sku="FREE-SAMPLE", unit_price="0.00")
    )
    assert resp.status_code == 201
    assert Decimal(resp.json()["unit_price"]) == Decimal("0.00")


async def test_list_products_filters_and_isolates_merchants(client: AsyncClient) -> None:
    _, h1 = await create_merchant_and_headers(client, 1)
    _, h2 = await create_merchant_and_headers(client, 2)

    await client.post(
        "/products/",
        headers=h1,
        json=product_payload(sku="OIL-1L", category="Cooking", is_active=True),
    )
    await client.post(
        "/products/",
        headers=h1,
        json=product_payload(sku="SUGAR-2KG", category="Dry Goods", is_active=False),
    )
    await client.post(
        "/products/",
        headers=h2,
        json=product_payload(sku="FLOUR-2KG", category="Cooking", is_active=True),
    )

    all_m1 = (await client.get("/products/", headers=h1)).json()
    assert [p["sku"] for p in all_m1] == ["OIL-1L", "SUGAR-2KG"]

    cooking = (await client.get("/products/", headers=h1, params={"category": "Cooking"})).json()
    assert [p["sku"] for p in cooking] == ["OIL-1L"]

    active = (await client.get("/products/", headers=h1, params={"active_only": True})).json()
    assert [p["sku"] for p in active] == ["OIL-1L"]

    assert (await client.get("/products/", headers=h1, params={"limit": 0})).status_code == 422
    assert (await client.get("/products/", headers=h1, params={"limit": 101})).status_code == 422
    assert (await client.get("/products/", headers=h1, params={"offset": -1})).status_code == 422
    assert (await client.get("/products/", headers=h1, params={"category": ""})).status_code == 422


async def test_list_products_pagination_slices_in_id_order(client: AsyncClient) -> None:
    _, headers = await create_merchant_and_headers(client)
    for i in range(5):
        resp = await client.post(
            "/products/",
            headers=headers,
            json=product_payload(sku=f"SKU-{i:02d}", name=f"Product {i}"),
        )
        assert resp.status_code == 201

    page_1 = (
        await client.get("/products/", headers=headers, params={"limit": 2, "offset": 0})
    ).json()
    page_2 = (
        await client.get("/products/", headers=headers, params={"limit": 2, "offset": 2})
    ).json()
    max_limit = (await client.get("/products/", headers=headers, params={"limit": 100})).json()
    beyond = (await client.get("/products/", headers=headers, params={"offset": 10})).json()

    assert [p["sku"] for p in page_1] == ["SKU-00", "SKU-01"]
    assert [p["sku"] for p in page_2] == ["SKU-02", "SKU-03"]
    assert len(max_limit) == 5
    assert beyond == []


async def test_get_and_update_product_enforces_ownership(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, h1 = await create_merchant_and_headers(client, 1)
    _, h2 = await create_merchant_and_headers(client, 2)

    created = (await client.post("/products/", headers=h1, json=product_payload())).json()
    pid = created["id"]

    # Owner can get and patch
    assert (await client.get("/products/0", headers=h1)).status_code == 422
    assert (await client.get(f"/products/{pid}", headers=h1)).status_code == 200
    patched = await client.patch(
        f"/products/{pid}",
        headers=h1,
        json={"unit_price": "350.00", "stock_qty": 40, "is_active": False},
    )
    assert patched.status_code == 200
    assert Decimal(patched.json()["unit_price"]) == Decimal("350.00")
    assert patched.json()["stock_qty"] == 40
    assert patched.json()["is_active"] is False

    # Another merchant gets 404 (no existence leak)
    assert (await client.get(f"/products/{pid}", headers=h2)).status_code == 404
    assert (
        await client.patch(f"/products/{pid}", headers=h2, json={"name": "Hijacked"})
    ).status_code == 404

    # Empty PATCH is a no-op returning current state
    empty_patch = await client.patch(f"/products/{pid}", headers=h1, json={})
    assert empty_patch.status_code == 200
    assert empty_patch.json()["stock_qty"] == 40

    # Reject explicit null, unknown fields, immutable fields, and stock below reserved
    assert (
        await client.patch(f"/products/{pid}", headers=h1, json={"name": None})
    ).status_code == 422
    assert (
        await client.patch(f"/products/{pid}", headers=h1, json={"unknown": 1})
    ).status_code == 422
    assert (
        await client.patch(f"/products/{pid}", headers=h1, json={"sku": "NEW-SKU"})
    ).status_code == 422
    assert (
        await client.patch(f"/products/{pid}", headers=h1, json={"merchant_id": 999})
    ).status_code == 422

    await db.execute(update(Product).where(Product.id == pid).values(reserved_qty=10))
    await db.commit()
    low_stock = await client.patch(f"/products/{pid}", headers=h1, json={"stock_qty": 9})
    assert low_stock.status_code == 409
    exact_reserved = await client.patch(f"/products/{pid}", headers=h1, json={"stock_qty": 10})
    assert exact_reserved.status_code == 200
    assert exact_reserved.json()["available_qty"] == 0


async def test_adjust_product_stock_endpoint(client: AsyncClient, db: AsyncSession) -> None:
    _, h1 = await create_merchant_and_headers(client, 1)
    _, h2 = await create_merchant_and_headers(client, 2)

    created = (
        await client.post("/products/", headers=h1, json=product_payload(stock_qty=20))
    ).json()
    pid = created["id"]

    restock = await client.post(f"/products/{pid}/stock", headers=h1, json={"delta": 15})
    assert restock.status_code == 200
    assert restock.json()["stock_qty"] == 35

    writeoff = await client.post(f"/products/{pid}/stock", headers=h1, json={"delta": -5})
    assert writeoff.status_code == 200
    assert writeoff.json()["stock_qty"] == 30

    assert (
        await client.post(f"/products/{pid}/stock", headers=h1, json={"delta": 0})
    ).status_code == 422
    assert (
        await client.post(f"/products/{pid}/stock", headers=h1, json={"delta": 1_000_000})
    ).status_code == 422
    assert (
        await client.post(f"/products/{pid}/stock", headers=h1, json={"delta": -1_000_000})
    ).status_code == 422
    assert (
        await client.post(f"/products/{pid}/stock", headers=h2, json={"delta": 5})
    ).status_code == 404

    await db.execute(update(Product).where(Product.id == pid).values(reserved_qty=25))
    await db.commit()
    below_reserved = await client.post(f"/products/{pid}/stock", headers=h1, json={"delta": -6})
    assert below_reserved.status_code == 409
    exact_reserved = await client.post(f"/products/{pid}/stock", headers=h1, json={"delta": -5})
    assert exact_reserved.status_code == 200
    assert exact_reserved.json()["available_qty"] == 0
