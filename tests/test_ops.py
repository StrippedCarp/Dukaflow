import asyncio
import importlib
import io
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import main as main_module
import services.mpesa as mpesa_service
import services.scheduler as scheduler_module
from core.config import Settings, settings
from core.database import get_db
from core.observability import (
    JsonLogFormatter,
    configure_logging,
    init_sentry,
    request_id_ctx,
    resolve_request_id,
)
from core.rate_limit import enforce_rate_limit, extract_client_ip
from core.redis import get_redis, redis_pool
from main import app, lifespan, request_id_middleware
from models import Order, OrderItem, OrderStatus, Product
from services.mpesa import expire_all_stale_orders
from services.scheduler import SWEEP_LOCK_KEY, run_stale_order_sweep_tick, stale_order_sweep_loop
from tests.test_orders import register_merchant_headers


def test_settings_operational_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(rate_limit_merchant_register_per_minute=0)
    with pytest.raises(ValidationError):
        Settings(rate_limit_webhook_per_minute=0)
    with pytest.raises(ValidationError):
        Settings(stale_order_max_age_minutes=0)
    disabled = Settings(stale_order_sweep_interval_seconds=0)
    assert disabled.stale_order_sweep_interval_seconds == 0


async def test_request_id_middleware_and_json_logging(client: AsyncClient) -> None:
    generated_resp = await client.get("/health")
    assert generated_resp.status_code == 200
    generated_id = generated_resp.headers["X-Request-ID"]
    assert len(generated_id) == 32
    int(generated_id, 16)
    assert request_id_ctx.get() == "-"

    echoed_resp = await client.get("/health", headers={"X-Request-ID": "req-abc_123.4"})
    assert echoed_resp.status_code == 200
    assert echoed_resp.headers["X-Request-ID"] == "req-abc_123.4"

    bad_http_resp = await client.get("/health", headers={"X-Request-ID": "bad id with spaces"})
    assert bad_http_resp.status_code == 200
    assert bad_http_resp.headers["X-Request-ID"] != "bad id with spaces"
    assert len(bad_http_resp.headers["X-Request-ID"]) == 32

    assert resolve_request_id("") != ""
    assert resolve_request_id("a") == "a"
    assert resolve_request_id("x" * 64) == "x" * 64
    assert resolve_request_id("bad\nid") != "bad\nid"
    assert resolve_request_id("x" * 65) != "x" * 65

    assert configure_logging("debug").level == logging.DEBUG
    assert configure_logging("BOGUS").level == logging.INFO
    assert configure_logging("getLogger").level == logging.INFO

    logger = configure_logging("INFO")
    try:
        assert logger.name == "dukaflow"
        assert logger.propagate is False
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        stream_handler = logger.handlers[0]
        assert isinstance(stream_handler, logging.StreamHandler)
        assert isinstance(stream_handler.formatter, JsonLogFormatter)

        stream = io.StringIO()
        stream_handler.setStream(stream)

        # Service loggers under dukaflow.* inherit JSON formatting and correlation ID.
        service_logger = logging.getLogger("dukaflow.services.mpesa")
        service_logger.info("order processed")

        class BrokenDb:
            async def execute(self, _stmt: object) -> None:
                raise RuntimeError("db down")

        async def override_broken_db() -> AsyncIterator[BrokenDb]:
            yield BrokenDb()

        try:
            app.dependency_overrides[get_db] = override_broken_db
            err_resp = await client.get("/health/ready", headers={"X-Request-ID": "req-ready-503"})
        finally:
            app.dependency_overrides.clear()

        assert err_resp.status_code == 503
        assert err_resp.json() == {"detail": "Service dependency unavailable"}
        assert err_resp.headers["X-Request-ID"] == "req-ready-503"
        assert request_id_ctx.get() == "-"

        # Middleware resets request_id_ctx even when an unhandled exception escapes call_next.
        fake_req = SimpleNamespace(headers={"X-Request-ID": "req-boom"})

        async def failing_next(_req: object) -> object:
            raise RuntimeError("unhandled")

        with pytest.raises(RuntimeError, match="unhandled"):
            await request_id_middleware(fake_req, failing_next)  # type: ignore[arg-type]
        assert request_id_ctx.get() == "-"

        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 2

        info_record = json.loads(lines[0])
        assert datetime.fromisoformat(info_record["timestamp"]).tzinfo is not None
        assert info_record["level"] == "INFO"
        assert info_record["logger"] == "dukaflow.services.mpesa"
        assert info_record["request_id"] == "-"
        assert info_record["message"] == "order processed"
        assert "exc_info" not in info_record

        err_record = json.loads(lines[1])
        assert err_record["level"] == "ERROR"
        assert err_record["logger"] == "dukaflow"
        assert err_record["request_id"] == "req-ready-503"
        assert err_record["message"] == "Readiness check failed"
        assert "RuntimeError: db down" in err_record["exc_info"]
    finally:
        configure_logging(settings.log_level)


def test_init_sentry_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert init_sentry("") is False
    assert init_sentry("   ") is False

    requested_modules: list[str] = []

    def raise_missing(name: str) -> object:
        requested_modules.append(name)
        raise ImportError("no sentry_sdk")

    monkeypatch.setattr(importlib, "import_module", raise_missing)
    assert init_sentry("https://public@example.invalid/1") is False
    assert requested_modules == ["sentry_sdk"]

    captured: dict[str, object] = {}

    def fake_init(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: SimpleNamespace(init=fake_init),
    )
    assert init_sentry("  https://public@example.invalid/1  ") is True
    assert captured["dsn"] == "https://public@example.invalid/1"
    assert captured["environment"] == "production"
    assert captured["send_default_pii"] is False
    assert captured["include_local_variables"] is False
    assert captured["traces_sample_rate"] == 0.1


async def test_extract_client_ip_ttl_and_redis_fail_open() -> None:
    assert extract_client_ip(SimpleNamespace(client=None)) == "unknown"  # type: ignore[arg-type]
    assert extract_client_ip(SimpleNamespace(client=SimpleNamespace(host=""))) == "unknown"  # type: ignore[arg-type]
    assert (
        extract_client_ip(SimpleNamespace(client=SimpleNamespace(host="10.0.0.5")))  # type: ignore[arg-type]
        == "10.0.0.5"
    )

    class BrokenRedis:
        def pipeline(self, transaction: bool = True) -> object:
            raise RedisError("redis unreachable")

    # Redis outages fail open cleanly without raising 500s.
    await enforce_rate_limit(BrokenRedis(), "mpesa_webhook", "127.0.0.1", max_requests=1)  # type: ignore[arg-type]

    # Verify NX TTL stability and automatic reset after window expiration.
    redis_client = Redis(connection_pool=redis_pool)
    try:
        await enforce_rate_limit(redis_client, "test_ttl", "10.9.9.9", 1, window_seconds=2)
        ttl_after_first = await redis_client.ttl("ratelimit:test_ttl:10.9.9.9")
        assert 1 <= ttl_after_first <= 2

        await asyncio.sleep(1.1)
        with pytest.raises(Exception) as exc_info:
            await enforce_rate_limit(redis_client, "test_ttl", "10.9.9.9", 1, window_seconds=2)
        assert getattr(exc_info.value, "status_code", None) == 429
        # NX keeps original remaining TTL (~1s) rather than extending back to 2s.
        ttl_after_second = await redis_client.ttl("ratelimit:test_ttl:10.9.9.9")
        assert 0 <= ttl_after_second <= 1

        await asyncio.sleep(1.1)
        await enforce_rate_limit(redis_client, "test_ttl", "10.9.9.9", 1, window_seconds=2)
    finally:
        await redis_client.aclose()


async def test_merchant_registration_rate_limit_isolation_and_spoof_immunity(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_merchant_register_per_minute", 2)

    for idx in range(2):
        resp = await client.post(
            "/merchants/",
            json={
                "name": f"Rate Shop {idx}",
                "email": f"rateshop{idx}@example.com",
                "phone_number": f"071200000{idx}",
            },
        )
        assert resp.status_code == 201

    # Exceeding merchant signup limit returns 429 even if attacker spoofs X-Forwarded-For.
    limited = await client.post(
        "/merchants/",
        headers={"X-Forwarded-For": "203.0.113.99", "X-Request-ID": "req-429-test"},
        json={
            "name": "Rate Shop 2",
            "email": "rateshop2@example.com",
            "phone_number": "0712000002",
        },
    )
    assert limited.status_code == 429
    assert limited.headers["X-Request-ID"] == "req-429-test"
    retry_after = int(limited.headers["Retry-After"])
    assert 1 <= retry_after <= 60

    # Different peer IP is unaffected (IP isolation).
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("10.0.0.2", 5555)),
        base_url="http://test",
    ) as other_ip_client:
        other_ip_resp = await other_ip_client.post(
            "/merchants/",
            json={
                "name": "Other IP Shop",
                "email": "otherip@example.com",
                "phone_number": "0712000099",
            },
        )
        assert other_ip_resp.status_code == 201

    # Different scope (/webhooks/whatsapp GET) on the same IP is also unaffected.
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "test-verify-token",
        "hub.challenge": "9",
    }
    assert (await client.get("/webhooks/whatsapp", params=params)).status_code == 200


async def test_whatsapp_verify_rate_limit_and_fail_closed_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_webhook_per_minute", 2)
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "test-verify-token",
        "hub.challenge": "9",
    }
    assert (await client.get("/webhooks/whatsapp", params=params)).status_code == 200
    assert (await client.get("/webhooks/whatsapp", params=params)).status_code == 200

    limited = await client.get("/webhooks/whatsapp", params=params)
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60

    # Empty verify token fails closed even when caller sends empty hub.verify_token; max_length=256.
    monkeypatch.setattr(settings, "rate_limit_webhook_per_minute", 10)
    monkeypatch.setattr(settings, "whatsapp_verify_token", "")
    empty_token_resp = await client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "ok"},
    )
    assert empty_token_resp.status_code == 403

    long_challenge = await client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "x" * 257},
    )
    assert long_challenge.status_code == 422


async def test_whatsapp_post_and_mpesa_rate_limits_isolated(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_webhook_per_minute", 2)

    # Exhaust POST /webhooks/whatsapp independently
    assert (await client.post("/webhooks/whatsapp", json={})).status_code == 403
    assert (await client.post("/webhooks/whatsapp", json={})).status_code == 403
    wa_limited = await client.post("/webhooks/whatsapp", json={})
    assert wa_limited.status_code == 429
    assert 1 <= int(wa_limited.headers["Retry-After"]) <= 60

    # Exhausting WhatsApp must NOT starve M-Pesa payment callbacks from the same IP!
    assert (await client.post("/webhooks/mpesa/callback", json={})).status_code == 403
    assert (await client.post("/webhooks/mpesa/callback", json={})).status_code == 403
    mpesa_limited = await client.post("/webhooks/mpesa/callback", json={})
    assert mpesa_limited.status_code == 429
    assert 1 <= int(mpesa_limited.headers["Retry-After"]) <= 60


async def test_health_ready_ok_and_redis_failure(client: AsyncClient) -> None:
    ok_resp = await client.get("/health/ready")
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {"status": "ready", "database": "ok", "redis": "ok"}

    class BrokenRedis:
        async def ping(self) -> bool:
            raise RuntimeError("redis down")

    async def override_broken_redis() -> AsyncIterator[BrokenRedis]:
        yield BrokenRedis()

    try:
        app.dependency_overrides[get_redis] = override_broken_redis
        assert (await client.get("/health/ready")).status_code == 503
    finally:
        app.dependency_overrides.clear()


async def test_expire_all_stale_orders_scheduler_and_lifespan(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    m1_id, _ = await register_merchant_headers(client, 1)
    m2_id, _ = await register_merchant_headers(client, 2)

    p1 = Product(
        merchant_id=m1_id,
        sku="MAIZE-50KG",
        name="Maize 50KG",
        unit_price=Decimal("2500.00"),
        stock_qty=10,
        reserved_qty=3,
    )
    p2 = Product(
        merchant_id=m2_id,
        sku="RICE-25KG",
        name="Rice 25KG",
        unit_price=Decimal("3000.00"),
        stock_qty=10,
        reserved_qty=2,
    )
    db.add_all([p1, p2])
    await db.flush()

    m1_stale = Order(
        merchant_id=m1_id,
        customer_phone="254700000010",
        total_amount=Decimal("7500.00"),
        status=OrderStatus.PENDING,
        items=[
            OrderItem(
                product_id=p1.id,
                sku=p1.sku,
                product_name=p1.name,
                quantity=3,
                unit_price=p1.unit_price,
            )
        ],
    )
    m1_paid = Order(
        merchant_id=m1_id,
        customer_phone="254700000011",
        total_amount=Decimal("500.00"),
        status=OrderStatus.PAID,
        mpesa_receipt_number="REC_PAID_1",
    )
    m2_stale = Order(
        merchant_id=m2_id,
        customer_phone="254700000020",
        total_amount=Decimal("3000.00"),
        status=OrderStatus.PENDING,
        items=[
            OrderItem(
                product_id=p2.id,
                sku=p2.sku,
                product_name=p2.name,
                quantity=1,
                unit_price=p2.unit_price,
            )
        ],
    )
    m2_fresh = Order(
        merchant_id=m2_id,
        customer_phone="254700000021",
        total_amount=Decimal("3000.00"),
        status=OrderStatus.PENDING,
        items=[
            OrderItem(
                product_id=p2.id,
                sku=p2.sku,
                product_name=p2.name,
                quantity=1,
                unit_price=p2.unit_price,
            )
        ],
    )
    db.add_all([m1_stale, m1_paid, m2_stale, m2_fresh])
    await db.commit()

    stale_ts = datetime.now(UTC) - timedelta(minutes=20)
    await db.execute(
        text("UPDATE orders SET created_at = :ts WHERE id IN (:id1, :id2, :id3)"),
        {"ts": stale_ts, "id1": m1_stale.id, "id2": m1_paid.id, "id3": m2_stale.id},
    )
    await db.commit()

    redis_client = Redis(connection_pool=redis_pool)
    try:
        # Held leader lock skips the sweep safely without deleting another owner's lock
        await redis_client.set(SWEEP_LOCK_KEY, "held", ex=30)
        assert await run_stale_order_sweep_tick(max_age_minutes=15) == 0
        assert await redis_client.get(SWEEP_LOCK_KEY) == "held"
        await redis_client.delete(SWEEP_LOCK_KEY)

        expired = await run_stale_order_sweep_tick(max_age_minutes=15)
        assert expired == 2
        assert await redis_client.get(SWEEP_LOCK_KEY) is None
        assert await run_stale_order_sweep_tick(max_age_minutes=15) == 0
        assert await redis_client.get(SWEEP_LOCK_KEY) is None
    finally:
        await redis_client.aclose()

    m1_stale_id = m1_stale.id
    m1_paid_id = m1_paid.id
    m2_stale_id = m2_stale.id
    m2_fresh_id = m2_fresh.id
    p1_id = p1.id
    p2_id = p2.id

    db.expire_all()
    assert (await db.get(Order, m1_stale_id)).status == OrderStatus.FAILED  # type: ignore[union-attr]
    assert (await db.get(Order, m1_paid_id)).status == OrderStatus.PAID  # type: ignore[union-attr]
    assert (await db.get(Order, m2_stale_id)).status == OrderStatus.FAILED  # type: ignore[union-attr]
    assert (await db.get(Order, m2_fresh_id)).status == OrderStatus.PENDING  # type: ignore[union-attr]
    assert (await db.get(Product, p1_id)).reserved_qty == 0  # type: ignore[union-attr]
    assert (await db.get(Product, p2_id)).reserved_qty == 1  # type: ignore[union-attr]


async def test_scheduler_lock_safety_and_per_merchant_isolation(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Per-merchant fault isolation in expire_all_stale_orders
    ma_id, _ = await register_merchant_headers(client, 3)
    mb_id, _ = await register_merchant_headers(client, 4)

    oa = Order(
        merchant_id=ma_id,
        customer_phone="254700000031",
        total_amount=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )
    ob = Order(
        merchant_id=mb_id,
        customer_phone="254700000032",
        total_amount=Decimal("200.00"),
        status=OrderStatus.PENDING,
    )
    db.add_all([oa, ob])
    await db.commit()
    await db.execute(
        text("UPDATE orders SET created_at = :ts WHERE id IN (:id1, :id2)"),
        {"ts": datetime.now(UTC) - timedelta(minutes=20), "id1": oa.id, "id2": ob.id},
    )
    await db.commit()

    orig_expire = mpesa_service.expire_stale_orders
    ob_id = ob.id
    oa_id = oa.id

    async def flaky_expire(session: AsyncSession, merchant_id: int, max_age_minutes: int) -> int:
        if merchant_id == ma_id:
            raise RuntimeError("simulated merchant failure")
        return await orig_expire(session, merchant_id, max_age_minutes)

    monkeypatch.setattr(mpesa_service, "expire_stale_orders", flaky_expire)
    expired_count = await expire_all_stale_orders(db, max_age_minutes=15)
    assert expired_count == 1
    db.expire_all()
    assert (await db.get(Order, oa_id)).status == OrderStatus.PENDING  # type: ignore[union-attr]
    assert (await db.get(Order, ob_id)).status == OrderStatus.FAILED  # type: ignore[union-attr]

    # 2. If lock ownership changes mid-tick, finally leaves the new owner's lock intact.
    redis_client = Redis(connection_pool=redis_pool)
    try:

        async def hijack_lock(_db: AsyncSession, max_age_minutes: int = 15) -> int:
            await redis_client.set(SWEEP_LOCK_KEY, "other-worker-token", ex=30)
            return max_age_minutes - max_age_minutes

        monkeypatch.setattr(scheduler_module, "expire_all_stale_orders", hijack_lock)
        assert await run_stale_order_sweep_tick(max_age_minutes=15) == 0
        assert await redis_client.get(SWEEP_LOCK_KEY) == "other-worker-token"
        await redis_client.delete(SWEEP_LOCK_KEY)

        # 3. RedisError while releasing the lock is swallowed with a warning.
        async def noop_expire(_db: AsyncSession, max_age_minutes: int = 15) -> int:
            return 0

        monkeypatch.setattr(scheduler_module, "expire_all_stale_orders", noop_expire)

        async def broken_get(_self: Redis, _key: str) -> str:
            raise RedisError("redis blip on release")

        monkeypatch.setattr(Redis, "get", broken_get)
        assert await run_stale_order_sweep_tick(max_age_minutes=15) == 0
    finally:
        await redis_client.aclose()


async def test_sweep_loop_and_lifespan_assertions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Pre-set stop_event performs 0 ticks.
    already_stopped = asyncio.Event()
    already_stopped.set()
    zero_calls = 0

    async def count_tick(max_age_minutes: int = 15) -> int:
        nonlocal zero_calls
        zero_calls += 1
        return max_age_minutes

    monkeypatch.setattr(scheduler_module, "run_stale_order_sweep_tick", count_tick)
    await stale_order_sweep_loop(already_stopped, interval_seconds=0.01, max_age_minutes=25)
    assert zero_calls == 0

    # Loop forwards max_age_minutes, logs exceptions without crashing, and continues on timeout.
    stop_event = asyncio.Event()
    observed_ages: list[int] = []

    async def flaky_loop_tick(max_age_minutes: int = 15) -> int:
        observed_ages.append(max_age_minutes)
        if len(observed_ages) == 1:
            raise RuntimeError("transient loop boom")
        stop_event.set()
        return 0

    monkeypatch.setattr(scheduler_module, "run_stale_order_sweep_tick", flaky_loop_tick)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="dukaflow"):
        await stale_order_sweep_loop(stop_event, interval_seconds=0.01, max_age_minutes=25)
    assert observed_ages == [25, 25]
    assert "Stale order sweep tick failed" in caplog.text

    # Assert lifespan wires logging, Sentry, and sweep scheduler based on interval setting.
    events: list[tuple[str, object]] = []

    def fake_configure_logging(level: str) -> logging.Logger:
        events.append(("logging", level))
        return logging.getLogger("dukaflow")

    def fake_init_sentry(dsn: str, environment: str) -> bool:
        events.append(("sentry", (dsn, environment)))
        return False

    loop_done = False

    async def fake_sweep_loop(
        ev: asyncio.Event, interval_seconds: float, max_age_minutes: int
    ) -> None:
        nonlocal loop_done
        events.append(("loop_start", (interval_seconds, max_age_minutes)))
        await ev.wait()
        loop_done = True

    monkeypatch.setattr(main_module, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(main_module, "init_sentry", fake_init_sentry)
    monkeypatch.setattr(main_module, "stale_order_sweep_loop", fake_sweep_loop)
    monkeypatch.setattr(settings, "stale_order_sweep_interval_seconds", 45)
    monkeypatch.setattr(settings, "stale_order_max_age_minutes", 20)

    async with lifespan(app):
        await asyncio.sleep(0)
    assert loop_done is True
    assert events == [
        ("logging", settings.log_level),
        ("sentry", (settings.sentry_dsn, settings.sentry_environment)),
        ("loop_start", (45, 20)),
    ]

    events.clear()
    monkeypatch.setattr(settings, "stale_order_sweep_interval_seconds", 0)
    async with lifespan(app):
        await asyncio.sleep(0)
    assert ("loop_start", (0, 20)) not in events
    assert len(events) == 2
