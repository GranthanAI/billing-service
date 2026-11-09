from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENVIRONMENTS = ("development", "test", "staging", "production")


def normalise_environment(value: object) -> str:
    """strip + lowercase, "prod" -> "production"; anything but the four known names is an error."""
    env = str(value).strip().lower()
    if env == "prod":
        env = "production"
    if env not in ENVIRONMENTS:
        raise ValueError(
            f"ENVIRONMENT must be one of {', '.join(ENVIRONMENTS)} (or prod): {value!r}"
        )
    return env


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
    file_service_url: str = ""
    file_service_token: str = ""
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

    @field_validator("environment", mode="before")
    @classmethod
    def _normalise_environment(cls, v: object) -> str:
        return normalise_environment(v)

    payment_gateway: Literal["razorpay", "fake"] = "razorpay"
    payg_enabled: bool = False
    plans_file: str = "/app/config/plans.yaml"
    grace_days: int = 3
    # When set, the grace period is this many seconds and GRACE_DAYS is ignored (simulation runs).
    grace_period_seconds: int | None = Field(None, ge=1)
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
    # --- payment simulation (docs/Integrations plan section 5). Never in production. ---
    payment_simulation: bool = False
    allow_gateway_mode_switch: bool = False
    sim_month_seconds: int = Field(0, ge=0)  # 0: real calendar months
    sim_retry_interval: int = Field(86_400, ge=1)  # seconds between failed-renewal retries
    sim_fast_jobs: bool = False
    sim_refund_delay_seconds: int = Field(10, ge=0)
    sim_webhook_url: str = "http://localhost:8080/billing/v1/webhooks/razorpay"
    sim_webhook_delay_seconds: int = Field(1, ge=0)
    sim_webhook_duplicate_pct: int = Field(0, ge=0, le=100)
    sim_webhook_drop_pct: int = Field(0, ge=0, le=100)
    sim_webhook_reorder: bool = False
    frontend_origin: str = "http://localhost:5173"
    # --- transactional email (worker only) ---
    email_mode: Literal["console", "file", "smtp"] = "console"
    email_enabled: bool = True
    email_outbox_dir: str = "./.email-outbox"
    email_from: str = ""
    email_reply_to: str = ""
    support_email: str = "support@granthan.ai"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_security: Literal["auto", "starttls", "ssl", "none"] = "auto"


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
    if payment_gateway == "fake" and normalise_environment(environment) == "production":
        raise ValueError("PAYMENT_GATEWAY=fake is refused when ENVIRONMENT=production")


def check_simulation_allowed(simulation: bool, environment: str) -> None:
    """The payment simulator must never run in production."""
    if simulation and normalise_environment(environment) == "production":
        raise ValueError("PAYMENT_SIMULATION=true is refused when ENVIRONMENT=production")


def payment_mode(s: "Settings") -> str:
    """ "simulated" or "live": what /me and /healthz report."""
    return "simulated" if s.payment_simulation else "live"


def check_email_config(mode: str, environment: str, smtp_host: str, email_from: str) -> None:
    """console and file mail must never run in production; smtp needs a host and a sender."""
    if mode in ("console", "file") and normalise_environment(environment) == "production":
        raise ValueError(f"EMAIL_MODE={mode} is refused when ENVIRONMENT=production")
    if mode == "smtp" and not (smtp_host and email_from):
        raise ValueError("EMAIL_MODE=smtp needs SMTP_HOST and EMAIL_FROM")
