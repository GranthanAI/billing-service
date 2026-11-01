from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from billing.domain import ledger
from billing.domain.catalog import Interval, ModelGroup, PaygPrices
from billing.domain.money import Paise, split_gst
from billing.domain.proration import ChangeKind, NoChange, classify_change, unused_credit
from billing.domain.rating import LlmCall, ModelPrice, PriceBook, UnknownModelPrice, rate

D = Decimal
PRICES = PriceBook(
    {
        "claude-haiku-5-5": ModelPrice(D("0.10"), None, D("0.50")),
        "openai/gpt-oss-20b": ModelPrice(D("0.075"), None, D("0.30")),
        "cached-model": ModelPrice(D("1.00"), D("0.10"), D("2.00")),
    }
)
PAYG = PaygPrices(
    unit_prices={
        "fast": Paise(20),
        "smart": Paise(50),
        "best": Paise(400),
        "ultra": Paise(1200),
        "youtube_video": Paise(100),
        "manim_video": Paise(200),
    }
)


def call(model: str, purpose: str, i: int, o: int, cached: int = 0) -> LlmCall:
    return LlmCall("p", model, purpose, i, cached, o)


def test_workbook_example() -> None:
    calls = [
        call("claude-haiku-5-5", "answer", 6000, 700),
        call("openai/gpt-oss-20b", "request_analysis", 2000, 250),
        call("openai/gpt-oss-20b", "memory", 600, 100),
    ]
    r = rate(calls, PRICES, D("95"), ModelGroup.SMART, "plan", PAYG)
    assert r.cost == 118_750
    assert (r.billable_units, r.price) == (1, 0)


@pytest.mark.parametrize(("tokens", "units"), [(6000, 1), (32000, 1), (32001, 2), (70000, 3)])
def test_payg_units(tokens: int, units: int) -> None:
    r = rate(
        [call("claude-haiku-5-5", "answer", tokens, 10)],
        PRICES,
        D("95"),
        ModelGroup.SMART,
        "payg",
        PAYG,
    )
    assert r.billable_units == units
    assert r.price == units * 50


def test_best_at_70000_tokens() -> None:
    r = rate(
        [call("claude-haiku-5-5", "answer", 70000, 10)],
        PRICES,
        D("95"),
        ModelGroup.BEST,
        "payg",
        PAYG,
    )
    assert r.price == 1200


def test_cached_pricing() -> None:
    # 1000 in of which 400 cached: 600 * 1.00 + 400 * 0.10 + 100 * 2.00 = 840 -> $0.00084
    r = rate(
        [call("cached-model", "answer", 1000, 100, cached=400)],
        PRICES,
        D("100"),
        ModelGroup.FAST,
        "plan",
        PAYG,
    )
    assert r.cost == 84_000
    # no cached price: cached tokens fall back to the input price
    r2 = rate(
        [call("claude-haiku-5-5", "answer", 1000, 0, cached=400)],
        PRICES,
        D("100"),
        ModelGroup.FAST,
        "plan",
        PAYG,
    )
    assert r2.cost == 10_000


def test_unknown_model_and_answer_count() -> None:
    with pytest.raises(UnknownModelPrice):
        rate([call("nope", "answer", 1, 1)], PRICES, D("95"), ModelGroup.FAST, "plan", PAYG)
    with pytest.raises(ValueError):
        rate(
            [call("claude-haiku-5-5", "memory", 1, 1)],
            PRICES,
            D("95"),
            ModelGroup.FAST,
            "plan",
            PAYG,
        )
    two = [call("claude-haiku-5-5", "answer", 1, 1)] * 2
    with pytest.raises(ValueError):
        rate(two, PRICES, D("95"), ModelGroup.FAST, "plan", PAYG)


def test_llm_call_validation() -> None:
    with pytest.raises(ValueError):
        LlmCall("p", "m", "bogus", 1, 0, 1)
    with pytest.raises(ValueError):
        LlmCall("p", "m", "answer", -1, 0, 1)
    with pytest.raises(ValueError):
        LlmCall("p", "m", "answer", 5, 6, 1)


# --- proration ---------------------------------------------------------------------------

S = datetime(2026, 11, 1, tzinfo=UTC)
E = S + timedelta(days=30)


def test_unused_credit() -> None:
    assert unused_credit(Paise(49900), S, E, S + timedelta(days=15)) == 24950
    assert unused_credit(Paise(49900), S, E, S) == 49900
    assert unused_credit(Paise(49900), S, E, S - timedelta(days=5)) == 49900
    assert unused_credit(Paise(49900), S, E, E) == 0
    assert unused_credit(Paise(49900), S, E, E + timedelta(days=1)) == 0
    with pytest.raises(ValueError):
        unused_credit(Paise(1), E, S, S)
    with pytest.raises(ValueError):
        unused_credit(Paise(-1), S, E, S)


M, Y = Interval.MONTH, Interval.YEAR


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        ((1, M), (2, M), ChangeKind.UPGRADE),
        ((1, M), (1, Y), ChangeKind.UPGRADE),
        ((2, Y), (1, M), ChangeKind.DOWNGRADE),
        ((1, Y), (1, M), ChangeKind.DOWNGRADE),
        ((2, M), (1, M), ChangeKind.DOWNGRADE),
        ((1, M), (2, Y), ChangeKind.UPGRADE),
    ],
)
def test_classify(old: tuple[int, Interval], new: tuple[int, Interval], kind: ChangeKind) -> None:
    assert classify_change(*old, *new) is kind


def test_classify_no_change() -> None:
    with pytest.raises(NoChange):
        classify_change(1, M, 1, M)


# --- ledger ------------------------------------------------------------------------------

WALLET = UUID(int=42)
WCODE = f"liability:wallet:{WALLET}"


def pairs(tx: ledger.LedgerTransaction) -> list[tuple[str, int]]:
    return [(p.account_code, p.amount) for p in tx.postings]


def test_pro_monthly_charge() -> None:
    tx = ledger.subscription_charge(split_gst(Paise(49900), "10", "10"), M, "k")
    assert pairs(tx) == [
        (ledger.CLEARING, 49900),
        (ledger.REVENUE_SUBSCRIPTION, -42288),
        (ledger.GST_CGST, -3806),
        (ledger.GST_SGST, -3806),
    ]


def test_pro_yearly_charge_defers_revenue() -> None:
    tx = ledger.subscription_charge(split_gst(Paise(499000), "10", "29"), Y, "k")
    codes = [c for c, _ in pairs(tx)]
    assert ledger.DEFERRED_REVENUE in codes
    assert ledger.REVENUE_SUBSCRIPTION not in codes
    assert ledger.GST_IGST in codes
    assert ledger.GST_CGST not in codes


def test_topup() -> None:
    tx = ledger.wallet_topup(split_gst(Paise(49900), "10", "10"), WALLET, "k")
    assert pairs(tx) == [
        (ledger.CLEARING, 49900),
        (WCODE, -42288),
        (ledger.GST_CGST, -3806),
        (ledger.GST_SGST, -3806),
    ]


def test_best_message_debit() -> None:
    assert pairs(ledger.payg_usage(Paise(400), WALLET, "k")) == [
        (WCODE, 339),
        (ledger.REVENUE_PAYG, -339),
    ]


def test_expiry_and_recognition() -> None:
    assert pairs(ledger.credit_expiry(Paise(1000), WALLET, "k")) == [
        (WCODE, 1000),
        (ledger.REVENUE_BREAKAGE, -1000),
    ]
    assert pairs(ledger.revenue_recognition(Paise(500), "k")) == [
        (ledger.DEFERRED_REVENUE, 500),
        (ledger.REVENUE_SUBSCRIPTION, -500),
    ]


def test_settlement() -> None:
    tx = ledger.settlement(Paise(49900), Paise(998), Paise(180), "k")
    assert pairs(tx) == [
        (ledger.BANK, 48722),
        (ledger.GATEWAY_FEES, 998),
        (ledger.GST_INPUT, 180),
        (ledger.CLEARING, -49900),
    ]
    assert [c for c, _ in pairs(ledger.settlement(Paise(100), Paise(0), Paise(0), "k"))] == [
        ledger.BANK,
        ledger.CLEARING,
    ]
    with pytest.raises(ValueError):
        ledger.settlement(Paise(100), Paise(100), Paise(0), "k")


def test_refund_is_exact_negation() -> None:
    split = split_gst(Paise(49900), "10", "10")
    charge = ledger.subscription_charge(split, M, "k")
    refund = ledger.subscription_refund(split, M, "k")
    assert [(c, -a) for c, a in pairs(charge)] == pairs(refund)


def test_unbalanced_raises() -> None:
    bad = ledger.LedgerTransaction(
        ledger.LedgerKind.SETTLEMENT, "k", (ledger.Posting("a", 100), ledger.Posting("b", -99))
    )
    with pytest.raises(ledger.UnbalancedTransaction):
        ledger.assert_balanced(bad)
    with pytest.raises(ledger.UnbalancedTransaction):
        ledger.assert_balanced(
            ledger.LedgerTransaction(
                ledger.LedgerKind.SETTLEMENT, "k", (ledger.Posting("a", 0),) * 2
            )
        )
    with pytest.raises(ledger.UnbalancedTransaction):
        ledger.assert_balanced(
            ledger.LedgerTransaction(ledger.LedgerKind.SETTLEMENT, "k", (ledger.Posting("a", 1),))
        )
    with pytest.raises(ledger.UnbalancedTransaction):
        ledger.payg_usage(Paise(0), WALLET, "k")
