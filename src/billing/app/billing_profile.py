"""The customer's own billing details (email, GST state, GSTIN, legal name).

A change applies to FUTURE invoices only: every invoice stores the customer's state and GSTIN as they were when it was
issued, so a change (even a GSTIN change in the same month) never rewrites an invoice that exists. Each change is audited.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from billing.app.errors import CatalogInvalid
from billing.app.ports import UnitOfWork
from billing.domain.catalog import OwnerType
from billing.domain.india import STATE_NAMES, is_state_code

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
AUDIT_ACTION = "billing_profile.update"


@dataclass(frozen=True, slots=True)
class BillingProfile:
    billing_email: str | None
    state_code: str | None
    gstin: str | None
    legal_name: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "billing_email": self.billing_email,
            "state_code": self.state_code,
            "state_name": STATE_NAMES.get(self.state_code or ""),
            "gstin": self.gstin,
            "legal_name": self.legal_name,
        }


def gst_states() -> list[dict[str, str]]:
    return [{"code": c, "name": n} for c, n in sorted(STATE_NAMES.items())]


def validate(email: str, state_code: str, gstin: str | None, legal_name: str | None) -> list[str]:
    """Every problem at once, each naming its field (the frontend shows them inline)."""
    errors: list[str] = []
    if not EMAIL_RE.match(email or "") or len(email) > 254:
        errors.append("billing_email: is not a valid email address")
    if not is_state_code(state_code or ""):
        errors.append("state_code: is not a GST state code")
    if gstin:
        if not GSTIN_RE.match(gstin):
            errors.append("gstin: is not a valid GSTIN (15 characters)")
        elif is_state_code(state_code or "") and gstin[:2] != state_code:
            errors.append("gstin: the first two digits must equal the state code")
        if not (legal_name or "").strip():
            errors.append("legal_name: is required when a GSTIN is given")
    return errors


class BillingProfileService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow = uow_factory

    async def get(self, user_id: UUID) -> BillingProfile:
        async with self._uow() as uow:
            a = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
        if a is None:
            return BillingProfile(None, None, None, None)
        return BillingProfile(a.billing_email, a.state_code, a.gstin, a.legal_name)

    async def update(
        self, user_id: UUID, email: str, state_code: str, gstin: str | None, legal_name: str | None
    ) -> BillingProfile:
        gstin = (gstin or "").strip().upper() or None
        legal_name = (legal_name or "").strip() or None
        email = (email or "").strip()
        errors = validate(email, state_code, gstin, legal_name)
        if errors:
            raise CatalogInvalid(errors)  # a 422 that lists each field's problem
        async with self._uow() as uow:
            account = await uow.accounts.get_or_create(OwnerType.USER, user_id, email)
            before = BillingProfile(
                account.billing_email, account.state_code, account.gstin, account.legal_name
            )
            await uow.accounts.update_profile(
                account.id, email=email, state_code=state_code, gstin=gstin, legal_name=legal_name
            )
            after = BillingProfile(email, state_code, gstin, legal_name)
            if after != before:
                await uow.audit.record(
                    user_id, AUDIT_ACTION, "billing_account", str(account.id), "customer update",
                    before.as_json(), after.as_json(),
                )  # fmt: skip
        return after
