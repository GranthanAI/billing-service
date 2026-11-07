from decimal import Decimal
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
    # Extra secrets accepted while rotating, comma-separated (secrets must not contain commas).
    razorpay_webhook_secrets: str = ""
    # api-gateway proves itself to the REST routes with X-Gateway-Secret; comma-separated to rotate
    gateway_shared_secret: str = Field(..., min_length=1)
    sentry_dsn: str = ""
    # org-service (organisations). Without a URL nobody is a member of anything.
    org_service_url: str = ""
    org_service_token: str = ""
    org_directory_ttl_seconds: int = Field(60, ge=0)
    org_grace_days: int = Field(15, ge=0)  # an unpaid organisation invoice keeps access this long
    service_token_keys: str = Field(...)
    supplier_gstin: str = ""
    supplier_legal_name: str = "GranthanAI"
    supplier_address: str = ""
    object_store_endpoint: str = ""
    object_store_bucket: str = "billing-invoices"
    object_store_access_key: str = ""
    object_store_secret_key: str = ""
    object_store_region: str = "us-east-1"
    supplier_state_code: str = Field(..., pattern=r"^\d{2}$")
    gst_registered: bool = True
    invoice_sac_code: str = "998319"
    invoice_series: str = "GA"
    credit_note_series: str = "CN"
    bill_of_supply_series: str = "BS"
    environment: str = "development"
    payment_gateway: Literal["razorpay", "fake"] = "razorpay"
    payg_enabled: bool = False
    plans_file: str = "/app/config/plans.yaml"
    grace_days: int = 3
    checkout_abandon_hours: int = Field(24, ge=1)
    razorpay_total_count_month: int = Field(120, ge=1)
    razorpay_total_count_year: int = Field(10, ge=1)
    hold_ttl_seconds: int = 600
    day_counter_ttl_seconds: int = 172_800
    month_counter_ttl_seconds: int = 40 * 86_400
    grpc_port: int = 50051
    entitlement_l1_ttl_seconds: float = 5.0
    daily_ai_budget_inr: int = 3000
    manim_render_cost_usd: Decimal = Decimal("0.01")
    youtube_ingest_cost_inr: Decimal = Decimal("0.30")
    timezone_business: str = "Asia/Kolkata"


def webhook_secrets(s: "Settings") -> str:
    """The primary webhook secret plus any rotation secrets, as one comma-joined string."""
    extra = [x.strip() for x in s.razorpay_webhook_secrets.split(",") if x.strip()]
    return ",".join(dict.fromkeys([s.razorpay_webhook_secret, *extra]))


def gateway_secrets(s: "Settings") -> tuple[str, ...]:
    """The accepted X-Gateway-Secret values (more than one while rotating)."""
    parts = (x.strip() for x in s.gateway_shared_secret.split(","))
    return tuple(dict.fromkeys(p for p in parts if p))


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def check_gateway_allowed(payment_gateway: str, environment: str) -> None:
    """The in-memory fake gateway must never run in production."""
    if payment_gateway == "fake" and environment == "production":
        raise ValueError("PAYMENT_GATEWAY=fake is refused when ENVIRONMENT=production")
