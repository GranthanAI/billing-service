from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from billing.domain import ledger
from billing.domain.catalog import Interval, ModelGroup, PaygPrices
from billing.domain.entitlements import Grant, GrantSource, pick
from billing.domain.money import Paise, display_paise, round_half_up, split_gst, taxable_of
from billing.domain.proration import unused_credit
from billing.domain.rating import LlmCall, ModelPrice, PriceBook, rate
from billing.domain.subscription import (
    AuthorisationTimedOut,
    CancelledExternally,
    ChargeCaptured,
    ChargeFailed,
    DowngradeScheduled,
    GatewayHalted,
    GraceExpired,
    IllegalTransition,
    NextMandateCharged,
    PeriodEnded,
    Resumed,
    SubscriptionState,
    SubStatus,
    UpgradeCharged,
    UserCancelled,
    transition,
)
from tests.support import plans

T0 = datetime(2026, 11, 1, tzinfo=UTC)
states = st.sampled_from(["10", "29", "07", "99"])
gross_st = st.integers(min_value=0, max_value=10**9).map(Paise)


@given(gross_st, states, states, st.booleans())
def test_split_gst_parts(gross: Paise, sup: str, cust: str, registered: bool) -> None:
    s = split_gst(gross, sup, cust, registered)
    assert s.total == gross
    assert min(s.taxable, s.cgst, s.sgst, s.igst) >= 0
    if registered:
        assert abs(s.taxable * 118 - gross * 100) <= 59
    else:
        assert (s.cgst, s.sgst, s.igst) == (0, 0, 0)


@given(st.integers(min_value=0, max_value=10**15), st.integers(min_value=1, max_value=10**9))
def test_round_half_up_matches_decimal(n: int, d: int) -> None:
    expected = int((Decimal(n) / Decimal(d)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    assert round_half_up(n, d) == expected


WALLET = UUID(int=7)
pos = st.integers(min_value=1, max_value=10**8).map(Paise)
intervals = st.sampled_from(list(Interval))


@st.composite
def transactions(draw: st.DrawFn) -> ledger.LedgerTransaction:
    key = "k"
    kind = draw(st.sampled_from(["charge", "refund", "recog", "topup", "payg", "expiry", "settle"]))
    split = split_gst(draw(pos), draw(states), draw(states), draw(st.booleans()))
    if kind == "charge":
        return ledger.subscription_charge(split, draw(intervals), key)
    if kind == "refund":
        return ledger.subscription_refund(split, draw(intervals), key)
    if kind == "recog":
        return ledger.revenue_recognition(draw(pos), key)
    if kind == "topup":
        return ledger.wallet_topup(split, WALLET, key)
    if kind == "payg":
        return ledger.payg_usage(draw(pos), WALLET, key)
    if kind == "expiry":
        return ledger.credit_expiry(draw(pos), WALLET, key)
    gross = draw(st.integers(min_value=1000, max_value=10**8))
    fee = draw(st.integers(min_value=0, max_value=gross // 4))
    fee_gst = draw(st.integers(min_value=0, max_value=gross // 4))
    return ledger.settlement(Paise(gross), Paise(fee), Paise(fee_gst), key)


@given(st.lists(transactions(), min_size=1, max_size=20))
def test_ledger_builders_balance(txs: list[ledger.LedgerTransaction]) -> None:
    balances: dict[str, int] = defaultdict(int)
    for tx in txs:
        assert sum(p.amount for p in tx.postings) == 0
        assert all(p.amount != 0 for p in tx.postings)
        for p in tx.postings:
            balances[p.account_code] += p.amount
    assert sum(balances.values()) == 0
    assert sum(p.amount for tx in txs for p in tx.postings) == sum(balances.values())


@given(
    st.integers(min_value=0, max_value=10**8),
    st.integers(min_value=1, max_value=10**7),
    st.integers(min_value=-10, max_value=10**7),
    st.integers(min_value=0, max_value=10**6),
)
def test_unused_credit_bounds_and_monotonic(paid: int, total_s: int, off: int, delta: int) -> None:
    start, end = T0, T0 + timedelta(seconds=total_s)
    now = start + timedelta(seconds=off)
    a = unused_credit(Paise(paid), start, end, now)
    b = unused_credit(Paise(paid), start, end, now + timedelta(seconds=delta))
    assert 0 <= b <= a <= paid


grant_st = st.builds(
    Grant,
    source=st.sampled_from(list(GrantSource)),
    plan_version=st.sampled_from([plans.FREE, plans.PRO, plans.MAX, plans.ORG_PRO, plans.PAYG]),
)


@given(st.lists(grant_st, min_size=1, max_size=8))
def test_pick_properties(grants: list[Grant]) -> None:
    grants = [*grants, Grant(GrantSource.FREE, plans.FREE)]
    picked = pick(grants)
    overrides = [g for g in grants if g.source is GrantSource.OVERRIDE]
    if overrides:
        assert picked.source is GrantSource.OVERRIDE
        assert picked.plan_version.rank == max(g.plan_version.rank for g in overrides)
    else:
        paid = [g for g in grants if g.source in (GrantSource.ORG, GrantSource.SUBSCRIPTION)]
        if paid:
            assert picked in paid
            assert picked.plan_version.rank >= max(g.plan_version.rank for g in paid)


TRIGGERS = st.sampled_from(
    [
        ChargeCaptured(T0, T0 + timedelta(days=30), Paise(49900)),
        AuthorisationTimedOut(),
        ChargeFailed(),
        GraceExpired(),
        GatewayHalted(),
        UserCancelled(),
        CancelledExternally(),
        PeriodEnded(),
        UpgradeCharged(plans.MAX.id, "sub_up", T0, T0 + timedelta(days=30), Paise(149900)),
        DowngradeScheduled(plans.FREE.id, "sub_next"),
        NextMandateCharged(T0, T0 + timedelta(days=30), Paise(49900)),
        Resumed("sub_resume"),
    ]
)


@given(st.lists(st.tuples(TRIGGERS, st.integers(min_value=0, max_value=100)), max_size=40))
def test_random_trigger_sequences(steps: list[tuple[object, int]]) -> None:
    s = SubscriptionState(
        status=SubStatus.INCOMPLETE,
        plan_version_id=plans.PRO.id,
        interval=Interval.MONTH,
        gateway_subscription_id="sub_1",
    )
    now = T0
    for trig, hours in steps:
        now += timedelta(hours=hours)
        was_ended = s.status is SubStatus.ENDED
        try:
            s = transition(s, trig, now).state  # type: ignore[arg-type]
        except IllegalTransition:
            pass
        else:
            assert not was_ended
        in_grace = s.status in (SubStatus.PAST_DUE, SubStatus.SUSPENDED)
        assert (s.grace_until is not None) == in_grace


PRICES = PriceBook(
    {
        "a": ModelPrice(Decimal("0.10"), Decimal("0.01"), Decimal("0.50")),
        "b": ModelPrice(Decimal("0.075"), None, Decimal("0.30")),
    }
)
PAYG = PaygPrices({g.value: Paise(10) for g in ModelGroup})
tokens = st.integers(min_value=0, max_value=10**6)


@given(tokens, tokens, tokens, tokens, st.sampled_from(list(ModelGroup)))
def test_rate_plan_mode_and_monotonic(i: int, o: int, di: int, do: int, group: ModelGroup) -> None:
    def cost(inp: int, out: int) -> int:
        calls = [
            LlmCall("p", "a", "answer", inp, 0, out),
            LlmCall("p", "b", "memory", 100, 0, 10),
        ]
        r = rate(calls, PRICES, Decimal("95"), group, "plan", PAYG)
        assert r.price == 0
        assert r.billable_units == 1
        return int(r.cost)

    assert cost(i + di, o + do) >= cost(i, o)


# --- Stage G: wallet display amounts --------------------------------------------------------------

PACKS = (19900, 49900, 99900, 199900)
UNIT_PRICES = (20, 50, 100, 200, 400, 1200)  # fast, smart, youtube, manim, best, ultra


@given(pack=st.sampled_from(PACKS), prices=st.lists(st.sampled_from(UNIT_PRICES), max_size=400))
def test_displayed_balance_tracks_pack_minus_unit_prices(pack: int, prices: list[int]) -> None:
    """Each debit takes taxable_of(price), rounded on its own, so the displayed balance drifts
    from pack - sum(unit prices) by less than one paisa per debit. (The prompt's "within 1 paisa
    for any sequence" cannot hold: 399 Smart messages are 175 paise off. See reports/stage-G.md.)
    After zero or one debit it is within 1 paisa."""
    balance = int(taxable_of(Paise(pack)))
    assert int(display_paise(Paise(balance))) == pack
    spent = 0
    for debits, price in enumerate(prices, start=1):
        debit = int(taxable_of(Paise(price)))
        if balance < debit:
            break
        balance -= debit
        spent += price
        shown = int(display_paise(Paise(balance)))
        assert abs(shown - (pack - spent)) <= debits
        if debits == 1:
            assert abs(shown - (pack - spent)) <= 1


@given(taxable=st.integers(min_value=0, max_value=10**9))
def test_display_paise_is_monotone_and_never_below_taxable(taxable: int) -> None:
    shown = int(display_paise(Paise(taxable)))
    assert shown >= taxable
    assert int(display_paise(Paise(taxable + 1))) >= shown
    assert int(display_paise(Paise(taxable), registered=False)) == taxable
    assert abs(shown * 100 - taxable * 118) <= 50  # within half a paisa of the exact value
