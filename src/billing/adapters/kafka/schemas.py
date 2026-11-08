"""Kafka event schemas (JSON, Pydantic v2), matching the rest of the platform.

Events billing produces are strict (`extra="forbid"`) and carry the common envelope fields at
the top level. Events billing consumes are lenient (`extra="ignore"`): their producers may add
fields without breaking us. gRPC stays protobuf; only Kafka is JSON.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "1.0"
PRODUCER = "billing-service"


# --- produced ------------------------------------------------------------------------------


class Envelope(BaseModel):
    """The fields every produced event starts with."""

    model_config = ConfigDict(extra="forbid")

    event_type: str
    schema_version: str = SCHEMA_VERSION
    event_id: str
    occurred_at: datetime
    correlation_id: str = ""
    producer: str = PRODUCER


class SubscriptionChangedMsg(Envelope):
    billing_account_id: str
    owner_type: str
    owner_id: str
    subscription_id: str
    plan_code: str
    status: str
    previous_plan_code: str | None = None
    previous_status: str | None = None
    current_period_end: datetime | None = None


class PaymentFailedMsg(Envelope):
    billing_account_id: str
    subscription_id: str
    gateway_payment_id: str
    amount_paise: int
    reason: str
    next_retry_at: datetime | None = None


class RenewalUpcomingMsg(Envelope):
    billing_account_id: str
    owner_id: str
    plan_code: str
    amount_paise: int
    charge_at: datetime


class PersonalPlanRedundantMsg(Envelope):
    user_id: str
    billing_account_id: str
    personal_plan_code: str
    org_id: str
    org_plan_code: str


class CreditsExpiringMsg(Envelope):
    user_id: str
    billing_account_id: str
    lot_id: str
    remaining_paise: int
    remaining_taxable_paise: int
    expires_at: datetime


class StoragePaymentRequiredMsg(Envelope):
    user_id: str
    billing_account_id: str
    over_gb: int
    shortfall_paise: int
    month: str


class UsageThresholdReachedMsg(Envelope):
    user_id: str
    billing_account_id: str
    limit: str
    percent: int
    used: int
    allowed: int
    resets_at: datetime


class InvoiceIssuedMsg(Envelope):
    billing_account_id: str
    invoice_id: str
    number: str
    kind: str
    total_paise: int
    pdf_object_key: str | None = None


class EntitlementsChangedMsg(Envelope):
    user_id: str
    reason: str
    version_hash: str  # "sha256:..." an opaque marker that differs for every announced change


# --- consumed ------------------------------------------------------------------------------


class _Consumed(BaseModel):
    model_config = ConfigDict(extra="ignore")


class LlmCall(_Consumed):
    provider: str
    model: str
    purpose: str  # answer, request_analysis, memory, manim_plan, ...
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    usage_estimated: bool = False


class ChatResponseGenerated(_Consumed):
    """llm-service, schema 2.2. It has no event_id: `dedupe_key` stands in for one."""

    conversation_id: str
    user_id: str
    request_message_id: str
    response_message_id: str | None = None
    status: Literal["success", "error"]
    timestamp: datetime
    hold_id: str | None = None
    model_group: str | None = None
    model: str | None = None
    calls: list[LlmCall] = []

    @property
    def dedupe_key(self) -> str:
        return f"chat.response.generated:{self.request_message_id}"

    @property
    def occurred_at(self) -> datetime:
        return self.timestamp


class ChatResponseCancelled(_Consumed):
    conversation_id: str
    user_id: str
    request_message_id: str
    response_message_id: str | None = None
    status: Literal["cancelled"] = "cancelled"
    timestamp: datetime
    hold_id: str | None = None
    model_group: str | None = None
    model: str | None = None
    calls: list[LlmCall] = []

    @property
    def dedupe_key(self) -> str:
        return f"chat.response.cancelled:{self.request_message_id}"

    @property
    def occurred_at(self) -> datetime:
        return self.timestamp


class ArtifactManimRendered(_Consumed):
    event_id: str
    occurred_at: datetime
    user_id: str
    job_id: str
    attempt: int = 1  # llm-service retries a render with attempt + 1; absent means 1
    hold_id: str | None = None
    status: Literal["success", "error"]
    calls: list[LlmCall] = []

    @property
    def dedupe_key(self) -> str:
        return self.event_id

    @property
    def usage_source_id(self) -> str:
        # Usage is deduplicated on (kind, source_id): a retry that succeeds after a failed first
        # try must not be dropped as a duplicate, so the attempt is part of the id.
        return f"{self.job_id}:{self.attempt}"


class FileYoutubeIngested(_Consumed):
    event_id: str
    occurred_at: datetime
    user_id: str
    source_id: str
    document_id: str | None = None
    attempt: int | None = None  # file-service retries an import with attempt + 1; absent means 1
    hold_id: str | None = None
    status: Literal["success", "error"]

    @property
    def dedupe_key(self) -> str:
        return self.event_id

    @property
    def usage_source_id(self) -> str:
        # "{document_id}:{attempt}", attempt 1 when absent (see ArtifactManimRendered)
        attempt = self.attempt or 1
        if self.document_id:
            return f"{self.document_id}:{attempt}"
        return self.source_id if ":" in self.source_id else f"{self.source_id}:{attempt}"


class UsageMetered(_Consumed):
    """usage.metered 1.0: a paid call by a service that needs no limit, only its cost recorded."""

    event_id: str
    occurred_at: datetime
    user_id: str
    producer: str = ""
    conversation_id: str | None = None
    request_message_id: str | None = None
    feature: str = ""
    calls: list[LlmCall] = []
    fixed_cost_micro_inr: int = 0

    @property
    def dedupe_key(self) -> str:
        return f"usage.metered:{self.event_id}"


class UserDeleted(_Consumed):
    event_id: str
    occurred_at: datetime
    user_id: str

    @property
    def dedupe_key(self) -> str:
        return self.event_id


class OrgMemberEvent(_Consumed):
    event_id: str
    occurred_at: datetime
    org_id: str
    user_id: str
    role: str = "member"
    org_type: str = "company"

    @property
    def dedupe_key(self) -> str:
        return self.event_id


class OrgMemberJoined(OrgMemberEvent):
    pass


class OrgMemberLeft(OrgMemberEvent):
    pass


ConsumedEvent = (
    OrgMemberJoined
    | OrgMemberLeft
    | ChatResponseGenerated
    | ChatResponseCancelled
    | ArtifactManimRendered
    | FileYoutubeIngested
    | UsageMetered
    | UserDeleted
)

CONSUMED_BY_TOPIC: dict[str, type[_Consumed]] = {
    "chat.response.generated": ChatResponseGenerated,
    "chat.response.cancelled": ChatResponseCancelled,
    "artifact.manim.rendered": ArtifactManimRendered,
    "file.youtube.ingested": FileYoutubeIngested,
    "usage.metered": UsageMetered,
    "user.deleted": UserDeleted,
    "org.member.joined": OrgMemberJoined,
    "org.member.left": OrgMemberLeft,
}


def decode_consumed(topic: str, payload: bytes) -> ConsumedEvent:
    """Raises KeyError for an unknown topic and pydantic.ValidationError for a malformed body;
    the consumer sends both to the dead-letter topic."""
    model = CONSUMED_BY_TOPIC[topic]
    event: ConsumedEvent = model.model_validate_json(payload)  # type: ignore[assignment]
    return event
