"""Stage G: an organisation's wallet pays for its members' overflow, with a per-member cap."""

from datetime import timedelta
from uuid import UUID, uuid4

from billing.adapters.valkey import keys
from billing.app.admin import Staff
from billing.app.errors import OrgDirectoryUnavailable
from billing.app.records import CreditLot, OrgInfo, PaymentRecord
from billing.app.reserve import CheckAndReserveRequest
from billing.domain.catalog import DenyReason, ModelGroup, OrgVariant, OwnerType, ReserveKind
from billing.domain.ledger import wallet_topup
from billing.domain.money import Paise, split_gst, taxable_of
from tests.integration.billing_env import Env
from tests.integration.test_orgs import pay, subscribe
from tests.integration.test_wallet import services, spend, top_up

CAP = 3000  # GST-inclusive paise per member and month: two Ultra messages (1,200 each)
ULTRA_TAXABLE = int(taxable_of(Paise(1200)))  # 1,017
FUNDED = 500_000  # GST-inclusive, as invoiced


def org_with_cap(env: Env, cap: int | None) -> OrgInfo:
    org = OrgInfo(
        uuid4(), "Acme Pvt Ltd", OrgVariant.COMPANY, "10BBBBB1111B1Z5", "ap@acme.test", "10", cap
    )
    env.orgs.add_org(org)
    return org


async def fund_org_wallet(env: Env, org_id: UUID, gross: int = FUNDED) -> UUID:
    """Credit an organisation's wallet directly (the real flow is the org top-up routes)."""
    now = env.clock.now()
    async with env.uow() as uow:
        account = await uow.accounts.get_by_owner(OwnerType.ORG, org_id)
        assert account is not None
        wallet = await uow.wallets.ensure(account.id)
        payment_id = uuid4()
        await uow.payments.insert(
            PaymentRecord(
                id=payment_id, billing_account_id=account.id, purpose="topup",
                gateway_payment_id=f"pay_{payment_id}", amount_paise=Paise(gross),
                status="captured", captured_at=now, raw={}, gateway_order_id=None, method="test",
            )
        )  # fmt: skip
        split = split_gst(Paise(gross), "10", "10")
        await uow.ledger.post(
            wallet_topup(split, account.id, f"fund:{payment_id}"), "payment", str(payment_id), now
        )
        await uow.wallets.add_lot(
            CreditLot(
                uuid4(), wallet.ledger_account_id, payment_id, split.taxable, split.taxable,
                now + timedelta(days=365),
            )
        )  # fmt: skip
    await env.counters.wallet_credit(account.id, int(split.taxable))
    return account.id


async def member_spend(env: Env, user: UUID, n: int) -> str:
    reserve, usage, _ = services(env)
    return await spend(env, reserve, usage, n, user=user)


async def raw(env: Env, key: str) -> str | None:
    value: str | None = await env.counters._c.get(key)  # type: ignore[attr-defined]
    return value


async def test_member_cap_denies_one_member_not_the_other_and_resets_next_ist_month(
    env: Env,
) -> None:
    org = org_with_cap(env, CAP)
    await pay(env, (await subscribe(env, org)).invoice_id)
    a, b = uuid4(), uuid4()
    env.orgs.join(a, org.org_id, OrgVariant.COMPANY)
    env.orgs.join(b, org.org_id, OrgVariant.COMPANY)
    wallet = await fund_org_wallet(env, org.org_id)
    funded_taxable = int(split_gst(Paise(FUNDED), "10", "10").taxable)

    ent = await env.entitlements.resolve(a)
    assert ent.grant.source.value == "org"
    assert ent.overflow_wallet_account_id == wallet
    assert ent.member_spend_cap_paise == CAP

    assert await member_spend(env, a, 1) == "credits"
    assert await member_spend(env, a, 2) == "credits"
    reserve, _, _ = services(env)
    third = await reserve.check_and_reserve(
        CheckAndReserveRequest(a, "idem-a3", ReserveKind.MESSAGE, ModelGroup.ULTRA)
    )
    assert not third.allowed
    assert third.deny_reason is DenyReason.MEMBER_SPEND_CAP
    assert third.message_key == "credits.member_cap"

    assert await member_spend(env, b, 10) == "credits"  # B has not spent anything yet

    month_a = keys.member_month(str(wallet), a, env.clock.now())
    month_b = keys.member_month(str(wallet), b, env.clock.now())
    assert int(await raw(env, month_a) or -1) == 2 * ULTRA_TAXABLE == 2034
    assert int(await raw(env, month_b) or -1) == ULTRA_TAXABLE == 1017
    ttl = await env.counters._c.ttl(month_a)  # type: ignore[attr-defined]
    assert 39 * 86400 < ttl <= 40 * 86400
    mirror = await env.counters.wallet_get(wallet)
    assert mirror["balance"] == funded_taxable - 3 * 1017
    assert mirror.get("held", 0) == 0

    env.clock.advance(days=30)  # 1 Dec 2026, 10:15 IST: a new month, a new key
    assert await member_spend(env, a, 3) == "credits"
    month_a_dec = keys.member_month(str(wallet), a, env.clock.now())
    assert month_a_dec != month_a
    assert int(await raw(env, month_a_dec) or -1) == ULTRA_TAXABLE
    assert int(await raw(env, month_a) or -1) == 2034  # last month's key is left to expire


async def test_no_cap_means_no_member_key(env: Env) -> None:
    org = org_with_cap(env, None)
    await pay(env, (await subscribe(env, org)).invoice_id)
    a = uuid4()
    env.orgs.join(a, org.org_id, OrgVariant.COMPANY)
    wallet = await fund_org_wallet(env, org.org_id)
    for n in range(1, 5):
        assert await member_spend(env, a, n) == "credits"
    assert await raw(env, keys.member_month(str(wallet), a, env.clock.now())) is None


async def test_members_use_their_own_wallet_when_the_org_has_none_or_does_not_win(
    env: Env,
) -> None:
    org = org_with_cap(env, CAP)
    await pay(env, (await subscribe(env, org)).invoice_id)
    user = env.user
    env.orgs.join(user, org.org_id, OrgVariant.COMPANY)
    _, _, wallet_service = services(env)
    await top_up(env, wallet_service)  # a personal wallet
    personal = UUID(
        str(
            await env.scalar(
                "SELECT id FROM billing.billing_accounts "
                "WHERE owner_type = 'user' AND owner_id = :u",
                u=user,
            )
        )
    )
    await env.entitlements.invalidate(user)

    ent = await env.entitlements.resolve(user)  # the org has no wallet yet
    assert ent.overflow_wallet_account_id == personal and ent.member_spend_cap_paise is None

    org_wallet = await fund_org_wallet(env, org.org_id)
    await env.entitlements.invalidate(user)
    ent = await env.entitlements.resolve(user)  # the org's grant wins and it has a wallet
    assert ent.overflow_wallet_account_id == org_wallet and ent.member_spend_cap_paise == CAP

    # a personal override outranks the org's grant: the org's wallet is not used
    await env.admin.create_override(
        Staff(uuid4(), "billing_admin"), OwnerType.USER, user, "max", "gift",
        env.clock.now() + timedelta(days=5),
    )  # fmt: skip
    ent = await env.entitlements.resolve(user)
    assert ent.grant.source.value == "override"
    assert ent.overflow_wallet_account_id == personal and ent.member_spend_cap_paise is None


async def test_org_wallet_not_used_when_org_details_are_unavailable(env: Env) -> None:
    org = org_with_cap(env, CAP)
    await pay(env, (await subscribe(env, org)).invoice_id)
    a = uuid4()
    env.orgs.join(a, org.org_id, OrgVariant.COMPANY)
    await fund_org_wallet(env, org.org_id)
    original = env.orgs.org

    async def down(org_id: UUID) -> OrgInfo | None:
        raise OrgDirectoryUnavailable("down")

    env.orgs.org = down  # type: ignore[method-assign]
    try:
        ent = await env.entitlements.resolve(a)  # the cap is unknown, so no uncapped spending
        assert ent.overflow_wallet_account_id is None
    finally:
        env.orgs.org = original  # type: ignore[method-assign]


async def test_hold_of_an_org_member_on_a_personal_wallet_is_swept_back_to_zero(env: Env) -> None:
    org = org_with_cap(env, CAP)
    await pay(env, (await subscribe(env, org)).invoice_id)  # the org has a plan but no wallet
    user = env.user
    env.orgs.join(user, org.org_id, OrgVariant.COMPANY)
    reserve, _, wallet_service = services(env)
    await top_up(env, wallet_service)  # personal credits
    personal = UUID(
        str(
            await env.scalar(
                "SELECT id FROM billing.billing_accounts "
                "WHERE owner_type = 'user' AND owner_id = :u",
                u=user,
            )
        )
    )
    await env.entitlements.invalidate(user)

    res = await reserve.check_and_reserve(
        CheckAndReserveRequest(user, "idem-sweep", ReserveKind.MESSAGE, ModelGroup.ULTRA)
    )
    assert res.allowed and res.funded_by == "credits" and res.billing_account_id == personal
    wallet_key = keys.wallet(str(personal))
    stored = await env.counters._c.hget(keys.hold(user, res.hold_id), "wkey")  # type: ignore[attr-defined]
    assert stored == wallet_key
    assert (await env.counters.wallet_get(personal))["held"] == ULTRA_TAXABLE

    env.clock.advance(seconds=601)  # the hold runs out
    await env.run_job("holds.sweep")

    mirror = await env.counters.wallet_get(personal)
    assert mirror["held"] == 0
    assert mirror["balance"] == int(taxable_of(Paise(19900)))  # nothing was charged
