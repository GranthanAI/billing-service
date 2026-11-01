from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read once at start; the process refuses to start if a required value is missing."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    billing_database_url: str = Field(...)
    billing_migration_database_url: str = Field(...)
    db_pool_size: int = 10
    billing_valkey_url: str = Field(...)
    kafka_bootstrap_servers: str = Field(..., min_length=1)
    kafka_client_id: str = "billing-service"
    job_concurrency: int = Field(4, ge=1)
    outbox_batch_size: int = Field(500, ge=1)
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
    environment: str = "development"
    payment_gateway: Literal["razorpay", "fake"] = "razorpay"
    payg_enabled: bool = False
    plans_file: str = "/app/config/plans.yaml"
    grace_days: int = 3
    hold_ttl_seconds: int = 600
    daily_ai_budget_inr: int = 3000
    timezone_business: str = "Asia/Kolkata"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def check_gateway_allowed(payment_gateway: str, environment: str) -> None:
    """The in-memory fake gateway must never run in production."""
    if payment_gateway == "fake" and environment == "production":
        raise ValueError("PAYMENT_GATEWAY=fake is refused when ENVIRONMENT=production")
