from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables / the .env file.

    Field names are matched case-insensitively, so DATABASE_URL in .env
    populates ``database_url``.
    """

    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    database_url: str
    redis_url: str
    whatsapp_verify_token: str
    whatsapp_app_secret: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    mpesa_base_url: str = "https://sandbox.safaricom.co.ke"
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_shortcode: str = "174379"
    mpesa_passkey: str = ""
    mpesa_callback_url: str = ""
    mpesa_webhook_secret: str = ""
    log_level: str = "INFO"
    sentry_dsn: str = ""
    sentry_environment: str = "production"
    rate_limit_merchant_register_per_minute: int = Field(default=10, ge=1)
    rate_limit_webhook_per_minute: int = Field(default=120, ge=1)
    stale_order_sweep_interval_seconds: int = Field(default=60, ge=0)
    stale_order_max_age_minutes: int = Field(default=15, ge=1)


settings = Settings()
