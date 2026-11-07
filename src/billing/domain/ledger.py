"""Double-entry posting builders. Debit positive, credit negative; every transaction sums to 0."""

from dataclasses import dataclass
from enum import StrEnum
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


class LedgerKind(StrEnum):
    SUBSCRIPTION_CHARGE = "subscription_charge"
    SUBSCRIPTION_REFUND = "subscription_refund"
    REVENUE_RECOGNITION = "revenue_recognition"
    WALLET_TOPUP = "wallet_topup"
    PAYG_USAGE = "payg_usage"
    CREDIT_EXPIRY = "credit_expiry"
    SETTLEMENT = "settlement"


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
    kind: LedgerKind
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


def subscription_charge(
    split: GstSplit, interval: Interval, key: str, *, received_in: str = CLEARING
) -> LedgerTransaction:
    """A customer paid. `received_in` is where the money sits: the Razorpay clearing account for
    card/UPI/payment-link payments (settled later), or the bank for a transfer."""
    revenue = REVENUE_SUBSCRIPTION if interval is Interval.MONTH else DEFERRED_REVENUE
    postings = (
        Posting(received_in, split.total),
        Posting(revenue, -split.taxable),
        *_tax_credits(split),
    )
    return assert_balanced(LedgerTransaction(LedgerKind.SUBSCRIPTION_CHARGE, key, postings))


def subscription_refund(split: GstSplit, interval: Interval, key: str) -> LedgerTransaction:
    charge = subscription_charge(split, interval, key)
    reversed_ = tuple(Posting(p.account_code, -p.amount) for p in charge.postings)
    return assert_balanced(LedgerTransaction(LedgerKind.SUBSCRIPTION_REFUND, key, reversed_))


def refund_settlement(gross: Paise, key: str) -> LedgerTransaction:
    """Razorpay took a refund out of a settlement: the cash left the bank while the clearing
    account still held the (already reversed) charge."""
    if gross <= 0:
        raise ValueError("a refund settlement must be positive")
    postings = (Posting(CLEARING, gross), Posting(BANK, -gross))
    return assert_balanced(LedgerTransaction(LedgerKind.SETTLEMENT, key, postings))


def revenue_recognition(amount: Paise, key: str) -> LedgerTransaction:
    postings = (Posting(DEFERRED_REVENUE, amount), Posting(REVENUE_SUBSCRIPTION, -amount))
    return assert_balanced(LedgerTransaction(LedgerKind.REVENUE_RECOGNITION, key, postings))


def wallet_topup(split: GstSplit, wallet_account: UUID, key: str) -> LedgerTransaction:
    postings = (
        Posting(CLEARING, split.total),
        Posting(wallet_code(wallet_account), -split.taxable),
        *_tax_credits(split),
    )
    return assert_balanced(LedgerTransaction(LedgerKind.WALLET_TOPUP, key, postings))


def payg_usage(price_incl_gst: Paise, wallet_account: UUID, key: str) -> LedgerTransaction:
    amount = taxable_of(price_incl_gst)
    postings = (Posting(wallet_code(wallet_account), amount), Posting(REVENUE_PAYG, -amount))
    return assert_balanced(LedgerTransaction(LedgerKind.PAYG_USAGE, key, postings))


def wallet_debit(taxable: Paise, wallet_account: UUID, key: str) -> LedgerTransaction:
    """Usage paid from credits, for an amount that is already the taxable value."""
    postings = (Posting(wallet_code(wallet_account), taxable), Posting(REVENUE_PAYG, -taxable))
    return assert_balanced(LedgerTransaction(LedgerKind.PAYG_USAGE, key, postings))


def credit_expiry(remaining: Paise, wallet_account: UUID, key: str) -> LedgerTransaction:
    postings = (
        Posting(wallet_code(wallet_account), remaining),
        Posting(REVENUE_BREAKAGE, -remaining),
    )
    return assert_balanced(LedgerTransaction(LedgerKind.CREDIT_EXPIRY, key, postings))


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
        LedgerTransaction(LedgerKind.SETTLEMENT, key, tuple(p for p in candidates if p.amount))
    )
