"""Checkout, verify, plan change, cancel and resume.

Each use case runs in units of work; none calls Razorpay inside a transaction. A use case that
needs the gateway reads in one transaction, calls out, then writes in a second one.
"""

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import structlog

from billing.app.config import BillingContext
from billing.app.context import correlation_id_var
from billing.app.effects import GATEWAY_CANCEL
from billing.app.errors import (
    ChangePending,
    InvalidSignature,
    PlanNotAllowed,
    SubscriptionStateError,
    ValidationFailed,
)
from billing.app.ports import GatewaySubscription, UnitOfWork
from billing.app.records import (
    BillingAccount,
    ChangeKind,
    ChangeRecord,
    ChangeStatus,
    PlanVersionRecord,
    SubscriptionRecord,
)
from billing.app.transitions import IllegalTransition, apply_transition
from billing.app.webhooks import EventApplier, GatewayEvent
from billing.domain.catalog import Interval, OwnerType
from billing.domain.india import is_state_code
from billing.domain.proration import ChangeKind as Direction
from billing.domain.proration import NoChange, classify_change
from billing.domain.subscription import (
    SubscriptionState,
    SubStatus,
    UserCancelled,
)

log = structlog.get_logger()

SELF_SERVE = frozenset({"pro", "max"})
NOT_SELF_SERVE = frozenset({"free", "payg", "org_pro", "org_max"})
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_START_LEAD = timedelta(minutes=10)  # Razorpay wants start_at in the future


@dataclass(frozen=True, slots=True)
class CheckoutOptions:
    """Everything the frontend hands to Razorpay Checkout."""

    subscription_id: UUID
    status: SubStatus
    key: str
    gateway_subscription_id: str
    name: str
    description: str
    email: str | None
    kind: str | None = None  # upgrade | downgrade | resume for plan changes
    change_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    subscription_id: UUID
    status: SubStatus


def _cause() -> str | None:
    return correlation_id_var.get() or None


def _word(interval: Interval) -> str:
    return "monthly" if interval is Interval.MONTH else "yearly"


def _plan_id(plan: PlanVersionRecord, interval: Interval) -> str:
    plan_id = plan.razorpay_plan_month if interval is Interval.MONTH else plan.razorpay_plan_year
    if not plan_id:
        raise ValidationFailed(f"{plan.plan.plan_code} has no {_word(interval)} price")
    return plan_id


class SubscriptionService:
    def __init__(self, bctx: BillingContext, applier: EventApplier) -> None:
        self._b = bctx
        self._applier = applier

    # --- helpers -------------------------------------------------------------------------

    def _options(
        self,
        sub: SubscriptionRecord,
        gateway_id: str,
        plan: PlanVersionRecord,
        interval: Interval,
        email: str | None,
        *,
        kind: str | None = None,
        change_id: UUID | None = None,
    ) -> CheckoutOptions:
        return CheckoutOptions(
            subscription_id=sub.id,
            status=sub.state.status,
            key=self._b.config.razorpay_key_id,
            gateway_subscription_id=gateway_id,
            name=self._b.config.brand_name,
            description=f"{plan.plan.plan_code.title()} · {_word(interval)}",
            email=email,
            kind=kind,
            change_id=change_id,
        )

    async def _self_serve_plan(self, uow: UnitOfWork, plan_code: str) -> PlanVersionRecord:
        if plan_code in NOT_SELF_SERVE:
            raise PlanNotAllowed(
                "Organisation plans are bought by an organisation admin."
                if plan_code.startswith("org_")
                else f"{plan_code} cannot be bought here."
            )
        plan = await uow.plans.current(plan_code, None) if plan_code in SELF_SERVE else None
        if plan is None:
            raise ValidationFailed(f"unknown plan {plan_code!r}")
        return plan

    async def _live_subscription(
        self, uow: UnitOfWork, user_id: UUID
    ) -> tuple[BillingAccount, SubscriptionRecord]:
        account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
        sub = await uow.subscriptions.live_for_account(account.id) if account else None
        if account is None or sub is None:
            raise SubscriptionStateError("You have no subscription.")
        return account, sub

    # --- checkout ------------------------------------------------------------------------

    async def checkout(
        self,
        user_id: UUID,
        plan_code: str,
        interval: Interval,
        state_code: str,
        email: str,
        legal_name: str | None = None,
        gstin: str | None = None,
    ) -> CheckoutOptions:
        if not is_state_code(state_code):
            raise ValidationFailed(f"unknown GST state code {state_code!r}")
        if not EMAIL_RE.match(email):
            raise ValidationFailed("email is not valid")
        if gstin is not None and not GSTIN_RE.match(gstin):
            raise ValidationFailed("gstin is not valid")
        cfg, now = self._b.config, self._b.clock.now()

        async with self._b.uow_factory() as uow:
            plan = await self._self_serve_plan(uow, plan_code)
            plan_id = _plan_id(plan, interval)
            account = await uow.accounts.get_or_create(OwnerType.USER, user_id, email)
            await uow.accounts.update_checkout(
                account.id, state_code=state_code, email=email, legal_name=legal_name, gstin=gstin
            )
            if await uow.subscriptions.live_for_account(account.id) is not None:
                raise SubscriptionStateError(
                    "You already have a subscription; use /subscription/change."
                )
            sub = await uow.subscriptions.find_reusable_incomplete(
                account.id,
                plan.plan.id,
                interval,
                now - timedelta(hours=cfg.abandon_hours),
            )
            if sub is None:
                sub = SubscriptionRecord(
                    id=self._b.ids.new(),
                    billing_account_id=account.id,
                    state=SubscriptionState(SubStatus.INCOMPLETE, plan.plan.id, interval),
                    created_at=now,
                )
                await uow.subscriptions.insert(sub)
                recovering = False
            else:
                recovering = sub.state.gateway_subscription_id is None
        if sub.state.gateway_subscription_id is not None:  # reuse: same Checkout options again
            return self._options(sub, sub.state.gateway_subscription_id, plan, interval, email)

        gw = await self._b.gateway.create_subscription(
            plan_id,
            total_count=cfg.total_count(interval),
            start_at=None,
            notes={
                "billing_subscription_id": str(sub.id),
                "billing_ref": str(sub.id),
                "user_id": str(user_id),
            },
            lookup_first=recovering,
        )
        async with self._b.uow_factory() as uow:
            locked = await uow.subscriptions.lock(sub.id)
            assert locked is not None
            if locked.state.gateway_subscription_id is None:
                locked = await uow.subscriptions.save(
                    replace(locked, state=replace(locked.state, gateway_subscription_id=gw.id))
                )
        return self._options(
            sub, locked.state.gateway_subscription_id or gw.id, plan, interval, email
        )

    # --- verify --------------------------------------------------------------------------

    async def verify_checkout(
        self, user_id: UUID, payment_id: str, subscription_id: str, signature: str
    ) -> VerifyResult:
        if not self._b.gateway.verify_checkout_signature(payment_id, subscription_id, signature):
            raise InvalidSignature()
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            sub = await uow.subscriptions.lock_by_gateway_id(subscription_id) if account else None
            if sub is None and account is not None:
                change = await uow.changes.by_gateway_id(subscription_id)
                sub = await uow.subscriptions.get(change.subscription_id) if change else None
            if account is None or sub is None or sub.billing_account_id != account.id:
                raise ValidationFailed("unknown subscription")
        fresh = await self._b.gateway.fetch_subscription(subscription_id)
        payment = await self._b.gateway.fetch_payment(payment_id)
        charged = payment.status == "captured" and fresh.paid_count > 0
        await self._applier.apply_gateway_event(
            GatewayEvent(
                event_id=f"verify:{payment_id}",
                type="subscription.charged" if charged else "subscription.authenticated",
                subscription=fresh,
                payment=payment if charged else None,
            )
        )
        async with self._b.uow_factory() as uow:
            current = await uow.subscriptions.get(sub.id)
        assert current is not None
        return VerifyResult(current.id, current.state.status)

    # --- plan change ---------------------------------------------------------------------

    async def change_plan(
        self, user_id: UUID, plan_code: str, interval: Interval
    ) -> CheckoutOptions:
        now = self._b.clock.now()
        async with self._b.uow_factory() as uow:
            target = await self._self_serve_plan(uow, plan_code)
            account, sub = await self._live_subscription(uow, user_id)
            if sub.state.status is not SubStatus.ACTIVE:
                raise SubscriptionStateError(
                    f"Your subscription is {sub.state.status.value}; plan changes need active."
                )
            if await uow.changes.open_for_subscription(sub.id) is not None:
                raise ChangePending("Finish or abandon your pending plan change first.")
            current = await uow.plans.by_id(sub.state.plan_version_id)
            assert current is not None
            try:
                direction = classify_change(
                    current.plan.rank, sub.state.interval, target.plan.rank, interval
                )
            except NoChange:
                raise PlanNotAllowed("You are already on this plan.") from None
            refund_payment = (
                await uow.payments.latest_for_subscription(sub.id)
                if direction is Direction.UPGRADE
                else None
            )
            plan_id = _plan_id(target, interval)
            period_end = sub.state.current_period_end
        if direction is Direction.DOWNGRADE and (
            period_end is None or period_end - now < MIN_START_LEAD
        ):
            raise SubscriptionStateError("Your period is about to end; try again after renewal.")
        start_at = period_end if direction is Direction.DOWNGRADE else None
        kind = ChangeKind.UPGRADE if direction is Direction.UPGRADE else ChangeKind.DOWNGRADE
        return await self._open_change(
            user_id,
            account,
            sub,
            kind,
            target,
            interval,
            plan_id,
            start_at,
            refund_payment.id if refund_payment else None,
            account.billing_email,
        )

    async def resume(self, user_id: UUID) -> CheckoutOptions:
        now = self._b.clock.now()
        async with self._b.uow_factory() as uow:
            account, sub = await self._live_subscription(uow, user_id)
            if sub.state.status is not SubStatus.CANCELLING:
                raise SubscriptionStateError("Only a cancelling subscription can be resumed.")
            if await uow.changes.open_for_subscription(sub.id) is not None:
                raise ChangePending("A resume is already waiting for authorisation.")
            plan = await uow.plans.by_id(sub.state.plan_version_id)
            assert plan is not None
            plan_id = _plan_id(plan, sub.state.interval)
            end = sub.state.current_period_end
        if end is None or end - now < MIN_START_LEAD:
            raise SubscriptionStateError("Your period is about to end; start a new checkout.")
        return await self._open_change(
            user_id,
            account,
            sub,
            ChangeKind.RESUME,
            plan,
            sub.state.interval,
            plan_id,
            end,
            None,
            account.billing_email,
        )

    async def _open_change(
        self,
        user_id: UUID,
        account: BillingAccount,
        sub: SubscriptionRecord,
        kind: ChangeKind,
        target: PlanVersionRecord,
        interval: Interval,
        plan_id: str,
        start_at: datetime | None,
        refund_payment_id: UUID | None,
        email: str | None,
    ) -> CheckoutOptions:
        ref = uuid4()
        gw = await self._b.gateway.create_subscription(
            plan_id,
            total_count=self._b.config.total_count(interval),
            start_at=start_at,
            notes={
                "billing_subscription_id": str(sub.id),
                "billing_ref": str(ref),
                "user_id": str(user_id),
                "change": kind.value,
            },
        )
        change = ChangeRecord(
            id=self._b.ids.new(),
            subscription_id=sub.id,
            kind=kind,
            new_plan_version_id=target.plan.id,
            new_interval=interval,
            gateway_subscription_id=gw.id,
            status=ChangeStatus.PENDING,
            refund_payment_id=refund_payment_id,
            created_at=self._b.clock.now(),
        )
        problem: Exception | None = None
        async with self._b.uow_factory() as uow:
            locked = await uow.subscriptions.lock(sub.id)
            valid_states = (
                {SubStatus.CANCELLING} if kind is ChangeKind.RESUME else {SubStatus.ACTIVE}
            )
            if locked is None or locked.state.status not in valid_states:
                problem = SubscriptionStateError("Your subscription changed; try again.")
            elif await uow.changes.open_for_subscription(sub.id) is not None:
                problem = ChangePending("Finish or abandon your pending plan change first.")
            else:
                await uow.changes.insert(change)
        if problem is not None:  # lost a race: the mandate we just created is an orphan
            await self._cancel_orphan(gw)
            raise problem
        return self._options(
            sub, gw.id, target, interval, email, kind=kind.value, change_id=change.id
        )

    async def _cancel_orphan(self, gw: GatewaySubscription) -> None:
        async with self._b.uow_factory() as uow:
            await uow.jobs.enqueue(
                GATEWAY_CANCEL,
                {"gateway_subscription_id": gw.id, "at_cycle_end": False},
                self._b.clock.now(),
                f"{GATEWAY_CANCEL}:{gw.id}:0",
            )

    # --- cancel --------------------------------------------------------------------------

    async def cancel(self, user_id: UUID) -> VerifyResult:
        async with self._b.uow_factory() as uow:
            _, found = await self._live_subscription(uow, user_id)
            sub = await uow.subscriptions.lock(found.id)
            assert sub is not None
            if sub.state.status not in (SubStatus.ACTIVE, SubStatus.PAST_DUE):
                raise SubscriptionStateError(
                    f"Your subscription is {sub.state.status.value}; there is nothing to cancel."
                )
            change = await uow.changes.open_for_subscription(sub.id)
            if change is not None or sub.state.next_gateway_subscription_id is not None:
                # a waiting mandate would otherwise start charging at the period end
                waiting = (
                    change.gateway_subscription_id
                    if change
                    else sub.state.next_gateway_subscription_id
                )
                if change:
                    await uow.changes.set_status(change.id, ChangeStatus.ABANDONED)
                assert waiting is not None
                await uow.jobs.enqueue(
                    GATEWAY_CANCEL,
                    {"gateway_subscription_id": waiting, "at_cycle_end": False},
                    self._b.clock.now(),
                    f"{GATEWAY_CANCEL}:{waiting}:0",
                )
                sub = replace(
                    sub,
                    state=replace(
                        sub.state,
                        next_gateway_subscription_id=None,
                        pending_plan_version_id=None,
                        pending_interval=None,
                    ),
                )
            try:
                saved = await apply_transition(
                    uow, self._b, sub, UserCancelled(), cause_ref=_cause()
                )
            except IllegalTransition as exc:
                raise SubscriptionStateError(str(exc)) from exc
        return VerifyResult(saved.id, saved.state.status)


__all__ = ["CheckoutOptions", "SubscriptionService", "VerifyResult"]
