from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from billing.domain.catalog import Interval, ModelGroup, PlanNotSoldForInterval
from billing.domain.entitlements import Entitlement, Grant, GrantSource, pick
from tests.support import plans

GROUPS = {
    "openai/gpt-oss-20b": ModelGroup.FAST,
    "openai/gpt-oss-120b": ModelGroup.SMART,
    "claude-haiku-5-5": ModelGroup.SMART,
    "claude-sonnet-5-5": ModelGroup.BEST,
}


def g(source: GrantSource, pv: object, **kw: object) -> Grant:
    return Grant(source, pv, **kw)  # type: ignore[arg-type]


def test_fixture_limits() -> None:
    assert plans.FREE.limits.messages_per_day == 25
    assert plans.PRO.limits.context_tokens == 64000
    assert plans.MAX.limits.best_per_day == 12
    assert plans.MAX.limits.branches_per_tree == -1


def test_limits_validation_and_helpers() -> None:
    with pytest.raises(ValueError):
        replace(plans.FREE_LIMITS, memory_facts=-2)
    lim = plans.MAX_LIMITS
    assert lim.allows("messages_per_day", 79)
    assert not lim.allows("messages_per_day", 80)
    assert lim.allows("branches_per_tree", 10**9)
    assert lim.remaining("branches_per_tree", 5) == -1
    assert lim.remaining("messages_per_day", 30) == 50
    assert lim.remaining("messages_per_day", 500) == 0
    with pytest.raises(ValueError):
        lim.allows("nope", 1)


def test_price_for_and_models() -> None:
    assert plans.PRO.price_for(Interval.MONTH) == 49900
    with pytest.raises(PlanNotSoldForInterval):
        plans.FREE.price_for(Interval.MONTH)
    with pytest.raises(PlanNotSoldForInterval):
        plans.ORG_PRO.price_for(Interval.MONTH)
    assert plans.FREE.allows_model("openai/gpt-oss-20b", GROUPS)
    assert plans.FREE.allows_model("openai/gpt-oss-120b", GROUPS)  # extra model
    assert not plans.FREE.allows_model("claude-haiku-5-5", GROUPS)
    assert plans.PRO.allows_model("claude-haiku-5-5", GROUPS)
    assert not plans.PRO.allows_model("claude-sonnet-5-5", GROUPS)
    assert plans.MAX.allows_model("claude-sonnet-5-5", GROUPS)
    assert not plans.MAX.allows_model("unknown-model", GROUPS)


def test_override_beats_higher_subscription() -> None:
    ov = g(GrantSource.OVERRIDE, plans.FREE)
    sub = g(GrantSource.SUBSCRIPTION, plans.MAX)
    assert pick([g(GrantSource.FREE, plans.FREE), sub, ov]) is ov


def test_override_tie_uses_latest_expiry() -> None:
    a = g(GrantSource.OVERRIDE, plans.PRO, expires_at=datetime(2026, 12, 1, tzinfo=UTC))
    b = g(GrantSource.OVERRIDE, plans.PRO, expires_at=datetime(2027, 1, 1, tzinfo=UTC))
    assert pick([a, b]) is b


def test_max_subscription_beats_org_pro() -> None:
    sub = g(GrantSource.SUBSCRIPTION, plans.MAX)
    org = g(GrantSource.ORG, plans.ORG_PRO)
    assert pick([org, sub, g(GrantSource.FREE, plans.FREE)]) is sub


def test_org_wins_tie() -> None:
    sub = g(GrantSource.SUBSCRIPTION, plans.PRO)
    org = g(GrantSource.ORG, plans.ORG_PRO)
    assert pick([sub, org]) is org
    assert pick([org, sub]) is org


def test_payg_only_without_paid() -> None:
    free = g(GrantSource.FREE, plans.FREE)
    payg = g(GrantSource.PAYG, plans.PAYG)
    assert pick([free, payg]) is payg
    sub = g(GrantSource.SUBSCRIPTION, plans.PRO)
    assert pick([free, payg, sub]) is sub


def test_free_default_and_errors() -> None:
    free = g(GrantSource.FREE, plans.FREE)
    assert pick([free]) is free
    with pytest.raises(ValueError):
        pick([])


def test_hash_stable_and_sensitive() -> None:
    sub = g(GrantSource.SUBSCRIPTION, plans.PRO, billing_account_id=UUID(int=9))
    a = Entitlement.of(sub)
    assert a.version_hash == Entitlement.of(sub).version_hash
    assert a.limits == plans.PRO.limits
    assert Entitlement.of(sub, UUID(int=7)).version_hash != a.version_hash
    assert Entitlement.of(g(GrantSource.SUBSCRIPTION, plans.MAX)).version_hash != a.version_hash
    other = g(GrantSource.SUBSCRIPTION, plans.PRO, billing_account_id=UUID(int=10))
    assert Entitlement.of(other).version_hash != a.version_hash
