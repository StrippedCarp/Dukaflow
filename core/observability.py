"""Structured JSON logging, request correlation IDs, and optional Sentry wiring."""

import importlib
import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
_VALID_REQUEST_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def resolve_request_id(header_value: str | None) -> str:
    """Validate an incoming X-Request-ID header or mint a fresh 32-char hex ID."""
    if header_value is not None and _VALID_REQUEST_ID_RE.fullmatch(header_value):
        return header_value
    return uuid.uuid4().hex


class JsonLogFormatter(logging.Formatter):
    """Render log records as single-line JSON with correlation ID context."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_ctx.get(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure the ``dukaflow`` logger to emit structured single-line JSON logs."""
    logger = logging.getLogger("dukaflow")
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level if isinstance(numeric_level, int) else logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    logger.handlers = [handler]
    logger.propagate = False
    return logger


def init_sentry(dsn: str, environment: str = "production") -> bool:
    """Optionally initialize Sentry SDK error tracking when ``dsn`` is configured."""
    cleaned_dsn = dsn.strip()
    if not cleaned_dsn:
        return False

    logger = logging.getLogger("dukaflow")
    try:
        sentry_sdk: Any = importlib.import_module("sentry_sdk")
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; skipping initialization")
        return False

    sentry_sdk.init(
        dsn=cleaned_dsn,
        environment=environment,
        send_default_pii=False,
        include_local_variables=False,
        traces_sample_rate=0.1,
    )
    logger.info("Sentry error tracking initialized for environment '%s'", environment)
    return True
