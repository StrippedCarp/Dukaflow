from httpx import AsyncClient


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.text == "Service is healthy"


def _verify_params(token: str = "test-verify-token", mode: str = "subscribe") -> dict[str, str]:
    return {"hub.mode": mode, "hub.verify_token": token, "hub.challenge": "1158201444"}


async def test_whatsapp_verify_echoes_challenge(client: AsyncClient) -> None:
    resp = await client.get("/webhooks/whatsapp", params=_verify_params())
    assert resp.status_code == 200
    # Meta requires the raw challenge back, not JSON ("\"1158201444\"").
    assert resp.text == "1158201444"
    assert resp.headers["content-type"].startswith("text/plain")


async def test_whatsapp_verify_rejects_wrong_token(client: AsyncClient) -> None:
    resp = await client.get("/webhooks/whatsapp", params=_verify_params(token="wrong"))
    assert resp.status_code == 403


async def test_whatsapp_verify_rejects_wrong_mode(client: AsyncClient) -> None:
    resp = await client.get("/webhooks/whatsapp", params=_verify_params(mode="unsubscribe"))
    assert resp.status_code == 403


async def test_whatsapp_verify_requires_params(client: AsyncClient) -> None:
    resp = await client.get("/webhooks/whatsapp")
    assert resp.status_code == 422
