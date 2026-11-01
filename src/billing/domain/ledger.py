"""Double-entry posting builders. Debit positive, credit negative; every transaction sums to 0."""

from dataclasses import dataclass
from uuid import UUID

from billing.domain.catalog import Interval
from billing.domain.money import GstSplit, Paise, taxable_of

CLEARING = "asset:razorpay_clearing"
BANK = "asset:bank"
GST_INPUT = "asset:gst_input"
GST_CGST = "liability:gst_output:cgst"
GST_SGST = "liability:gst_output:sgst"
GST_IGST = "liability:gst_output:igst"
DEFERRED_REVENUE = "liability:deferred_revenue"
REVENUE_SUBSCRIPTION = "revenue:subscription"
REVENUE_PAYG = "revenue:payg"
REVENUE_BREAKAGE = "revenue:breakage"
GATEWAY_FEES = "expense:gateway_fees"


def wallet_code(billing_account_id: UUID) -> str:
    return f"liability:wallet:{billing_account_id}"


class UnbalancedTransaction(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Posting:
    account_code: str
    amount: int


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    kind: str
    idempotency_key: str
    postings: tuple[Posting, ...]


def assert_balanced(tx: LedgerTransaction) -> LedgerTransaction:
    if len(tx.postings) < 2:
        raise UnbalancedTransaction("needs at least 2 postings")
    if any(p.amount == 0 for p in tx.postings):
        raise UnbalancedTransaction("zero-amount posting")
    if sum(p.amount for p in tx.postings) != 0:
        raise UnbalancedTransaction(f"postings do not sum to zero: {tx.kind}")
    return tx


def _tax_credits(split: GstSplit) -> tuple[Posting, ...]:
    parts = ((GST_CGST, split.cgst), (GST_SGST, split.sgst), (GST_IGST, split.igst))
    return tuple(Posting(code, -amt) for code, amt in parts if amt)


def subscription_charge(split: GstSplit, interval: Interval, key: str) -> LedgerTransaction:
    revenue = REVENUE_SUBSCRIPTION if interval is Interval.MONTH else DEFERRED_REVENUE
    postings = (
        Posting(CLEARING, split.total),
        Posting(revenue, -split.taxable),
        *_tax_credits(split),
    )
    return assert_balanced(LedgerTransaction("subscription_charge", key, postings))


def subscription_refund(split: GstSplit, interval: Interval, key: str) -> LedgerTransaction:
    charge = subscription_charge(split, interval, key)
    reversed_ = tuple(Posting(p.account_code, -p.amount) for p in charge.postings)
    return assert_balanced(LedgerTransaction("subscription_refund", key, reversed_))


def revenue_recognition(amount: Paise, key: str) -> LedgerTransaction:
    postings = (Posting(DEFERRED_REVENUE, amount), Posting(REVENUE_SUBSCRIPTION, -amount))
    return assert_balanced(LedgerTransaction("revenue_recognition", key, postings))


def wallet_topup(split: GstSplit, wallet_account: UUID, key: str) -> LedgerTransaction:
    postings = (
        Posting(CLEARING, split.total),
        Posting(wallet_code(wallet_account), -split.taxable),
        *_tax_credits(split),
    )
    return assert_balanced(LedgerTransaction("topup", key, postings))


def payg_usage(price_incl_gst: Paise, wallet_account: UUID, key: str) -> LedgerTransaction:
    amount = taxable_of(price_incl_gst)
    postings = (Posting(wallet_code(wallet_account), amount), Posting(REVENUE_PAYG, -amount))
    return assert_balanced(LedgerTransaction("payg_usage", key, postings))


def credit_expiry(remaining: Paise, wallet_account: UUID, key: str) -> LedgerTransaction:
    postings = (
        Posting(wallet_code(wallet_account), remaining),
        Posting(REVENUE_BREAKAGE, -remaining),
    )
    return assert_balanced(LedgerTransaction("credit_expiry", key, postings))


def settlement(gross: Paise, fee: Paise, fee_gst: Paise, key: str) -> LedgerTransaction:
    net = gross - fee - fee_gst
    if min(fee, fee_gst) < 0 or net <= 0:
        raise ValueError("fees must be >= 0 and smaller than the gross")
    candidates = (
        Posting(BANK, net),
        Posting(GATEWAY_FEES, fee),
        Posting(GST_INPUT, fee_gst),
        Posting(CLEARING, -gross),
    )
    return assert_balanced(
        LedgerTransaction("settlement", key, tuple(p for p in candidates if p.amount))
    )
