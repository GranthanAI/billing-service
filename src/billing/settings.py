from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read once at start; the process refuses to start if a required value is missing."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    billing_database_url: str = Field(...)
    billing_migration_database_url: str = Field(...)
    db_pool_size: int = 10
    billing_valkey_url: str = Field(...)
    kafka_bootstrap_servers: str = Field(...)
    razorpay_key_id: str = Field(...)
    razorpay_key_secret: str = Field(...)
    razorpay_webhook_secret: str = Field(...)
    service_token_keys: str = Field(...)
    supplier_gstin: str = ""
    supplier_state_code: str = Field(..., pattern=r"^\d{2}$")
    gst_registered: bool = True
    invoice_sac_code: str = "998319"
    invoice_series: str = "GA"
    credit_note_series: str = "CN"
    plans_file: str = "/app/config/plans.yaml"
    grace_days: int = 3
    hold_ttl_seconds: int = 600
    daily_ai_budget_inr: int = 3000
    timezone_business: str = "Asia/Kolkata"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
