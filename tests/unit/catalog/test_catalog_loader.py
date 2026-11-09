from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from billing.adapters.catalog.seed_loader import load_fx_rates, load_model_prices
from billing.adapters.catalog.yaml_loader import CatalogError, load_catalog, parse_catalog
from billing.app.catalog_spec import blocks
from billing.domain.catalog import ModelGroup, OrgVariant
from billing.domain.rating import ModelPrice, PriceBook

ROOT = Path(__file__).resolve().parents[3]
TEXT = (ROOT / "config" / "plans.yaml").read_text(encoding="utf-8")


def parse(text: str):  # type: ignore[no-untyped-def]
    return parse_catalog(yaml.safe_load(text))


def edit(old: str, new: str) -> str:
    assert old in TEXT, old
    return TEXT.replace(old, new, 1)


def checksums(text: str) -> dict[tuple[str, OrgVariant | None], str]:
    return {b.key: b.checksum() for b in blocks(parse(text))}


# --- the real file ---------------------------------------------------------------------------


def test_real_catalog_matches_the_workbook() -> None:
    spec = load_catalog(ROOT / "config" / "plans.yaml")
    assert spec.catalog_version == "2026-11-09"
    by = {b.key: b for b in blocks(spec)}
    free, pro, mx = by[("free", None)], by[("pro", None)], by[("max", None)]
    assert (free.price_month, pro.price_month, pro.price_year) == (None, 49900, 499000)
    assert (mx.price_month, mx.price_year) == (149900, 1499000)
    assert [b.limits.messages_per_day for b in (free, pro, mx)] == [25, 60, 80]
    assert mx.limits.best_per_day == 12
    assert [b.limits.context_tokens for b in (free, pro, mx)] == [16000, 64000, 128000]
    assert [b.limits.youtube_per_month for b in (free, pro, mx)] == [3, 20, 60]
    assert mx.limits.branches_per_tree == -1
    assert free.extra_models == ("openai/gpt-oss-120b",)
    assert set(by) >= {
        ("org_pro", OrgVariant.COLLEGE),
        ("org_pro", OrgVariant.COMPANY),
        ("org_max", OrgVariant.COLLEGE),
        ("org_max", OrgVariant.COMPANY),
        ("payg", None),
    }
    assert by[("org_pro", OrgVariant.COLLEGE)].min_seats == 50
    assert spec.payg.unit_prices["best"] == 400


def test_seed_prices_cover_exactly_the_catalog_models() -> None:
    spec = load_catalog(ROOT / "config" / "plans.yaml")
    seed = load_model_prices(ROOT / "config" / "seed" / "model_prices.yaml")
    # Every catalog model has a price. Price rows may exist for models that are deliberately not listed yet:
    # gemini-3.1-pro (preview only at Google; re-add it to plans.yaml when a GA id exists, no new price needed).
    unlisted_but_priced = {"gemini-3.1-pro"}
    assert {p.model for p in seed} - unlisted_but_priced == spec.all_models()
    assert unlisted_but_priced <= {p.model for p in seed}
    group_of = spec.group_of()
    assert all(group_of[p.model] == p.group for p in seed if p.model not in unlisted_but_priced)
    haiku = next(p for p in seed if p.model == "claude-haiku-5-5")
    assert (haiku.input_usd_per_m, haiku.output_usd_per_m) == (Decimal("0.10"), Decimal("0.50"))
    assert all(p.effective_from.utcoffset() is not None for p in seed)
    (fx,) = load_fx_rates(ROOT / "config" / "seed" / "fx_rates.yaml")
    assert fx.usd_inr == Decimal("95.0000")


def test_price_book_membership() -> None:
    book = PriceBook({"a": ModelPrice(Decimal(1), None, Decimal(2))})
    assert "a" in book
    assert "b" not in book
    assert book.models() == frozenset({"a"})


# --- validation: each rule has its own message -------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        # 1. limits
        ("  custom_modes: 3\n", "", "base_limits: limit 'custom_modes' is missing"),
        (
            "  file_size_mb: 10\n",
            "  file_size_mb: -5\n",
            "base_limits: limit 'file_size_mb' must be an int >= 0 or -1, got -5",
        ),
        (
            "  storage_mb: 100\n",
            "  storage_mb: lots\n",
            "base_limits: limit 'storage_mb' must be an int >= 0 or -1, got 'lots'",
        ),
        (
            "      custom_modes: 10\n",
            "      custom_modes: 10\n      bogus: 1\n",
            "plan 'pro': unknown limit 'bogus'",
        ),
        ("    limits: *free_limits\n", "", "plan 'free': limits is missing"),
        # 2. prices
        (
            "  - code: free\n    rank: 0\n",
            "  - code: free\n    rank: 0\n    prices: {month: 1, year: 2}\n",
            "plan 'free': the free plan must not have prices",
        ),
        (
            "prices: {month: 49900, year: 499000}",
            "prices: {month: 49900}",
            "plan 'pro': prices.year is required",
        ),
        (
            "prices: {month: 49900, year: 499000}",
            "prices: {year: 499000}",
            "plan 'pro': prices.month is required",
        ),
        (
            "prices: {month: 49900, year: 499000}",
            "prices: {month: -1, year: 499000}",
            "plan 'pro': prices.month must be >= 0 paise, got -1",
        ),
        (
            "prices: {month: 49900, year: 499000}",
            "prices: {month: 499.5, year: 499000}",
            "plan 'pro'",
        ),
        (
            "prices: {year: 298800}",
            "prices: {month: 1, year: 298800}",
            "plan 'org_pro' variant 'college': prices.month is not allowed",
        ),
        (
            "prices: {year: 298800}",
            "prices: {}",
            "plan 'org_pro' variant 'college': prices.year is required",
        ),
        (
            "min_seats: 50",
            "min_seats: 0",
            "plan 'org_pro' variant 'college': min_seats must be >= 1",
        ),
        # 3. best_per_day <= messages_per_day
        (
            "      messages_per_day: 60\n",
            "      messages_per_day: 60\n      best_per_day: 99\n",
            "plan 'pro': best_per_day (99) must not exceed messages_per_day (60)",
        ),
        # 4. unique codes and ranks per audience
        ("  - code: max\n", "  - code: pro\n", "plan 'pro': duplicate plan code"),
        (
            "  - code: max\n    rank: 2\n",
            "  - code: max\n    rank: 1\n",
            "plan 'max': rank 1 is already used by plan 'pro' in the individual audience",
        ),
        (
            "  - code: org_max                      # v2\n    rank: 2\n",
            "  - code: org_max                      # v2\n    rank: 1\n",
            "plan 'org_max': rank 1 is already used by plan 'org_pro' in the organisation college",
        ),
        # 5. models and groups
        (
            "  smart:\n    - {id: openai/gpt-oss-120b,",
            "    - {id: claude-haiku-5-5, label: X, provider: anthropic}\n"
            "  smart:\n    - {id: openai/gpt-oss-120b,",
            "model 'claude-haiku-5-5' is in more than one group (fast, smart)",
        ),
        (
            "extra: [openai/gpt-oss-120b]",
            "extra: [not-a-model]",
            "plan 'free': extra model 'not-a-model' is not in any model group",
        ),
        (
            "models: {groups: [fast, smart]}\n    limits: &pro_limits",
            "models: {groups: [fast, nope]}\n    limits: &pro_limits",
            "plan 'pro': unknown model group 'nope'",
        ),
        (
            "  ultra:\n",
            "  turbo:\n",
            "model_groups: unknown model group 'turbo'",
        ),
        (
            "label: GPT-6 Astra,           provider: openai",
            "label: '',                    provider: openai",
            "model 'gpt-6-astra' needs a label and a provider",
        ),
        (
            "    default_group: fast\n",
            "    default_group: smart\n",
            "plan 'free': default_group 'smart' is not one of the plan's model groups",
        ),
        (
            "    default_group: fast\n",
            "    default_group: turbo\n",
            "plan 'free': default_group: unknown model group 'turbo'",
        ),
        (
            "rank: 2\n    default_group: smart\n    prices: {month: 149900",
            "rank: 2\n    default_group: best\n    prices: {month: 149900",
            "default_group must not be 'best'",
        ),
        # 6. payg
        (", ultra: 1200", "", "plan 'payg': unit_prices is missing 'ultra'"),
        (
            "long_message_tokens: 32000",
            "long_message_tokens: 0",
            "plan 'payg': long_message_tokens must be > 0",
        ),
    ],
)
def test_each_rule_fails_with_a_precise_message(old: str, new: str, message: str) -> None:
    with pytest.raises(CatalogError) as exc:
        parse(edit(old, new))
    assert message in str(exc.value)


def test_unlimited_messages_skips_the_best_check() -> None:
    text = edit(
        "  messages_per_day: 25\n  best_per_day: 0\n", "  messages_per_day: -1\n  best_per_day: 5\n"
    )
    assert parse(text).base_limits.messages_per_day == -1


def test_structure_errors() -> None:
    with pytest.raises(CatalogError, match="is missing"):
        parse_catalog({"catalog_version": "x"} | {k: 1 for k in ("currency",)})
    with pytest.raises(CatalogError, match="top level"):
        parse_catalog([])
    raw = yaml.safe_load(TEXT)
    raw["plans"] = [p for p in raw["plans"] if p["code"] != "payg"]
    with pytest.raises(CatalogError, match="'payg' plan is missing"):
        parse_catalog(raw)
    with pytest.raises(CatalogError, match="cannot read"):
        load_catalog(ROOT / "nope.yaml")


# --- checksums ---------------------------------------------------------------------------------


def test_checksum_ignores_comments_order_and_whitespace() -> None:
    base = checksums(TEXT)
    noisy = TEXT.replace(
        "prices: {month: 49900, year: 499000}",
        "# a comment\n    prices:\n      year:    499000   # yearly\n      month: 49900",
    ).replace("  - code: max\n    rank: 2\n", "  - rank: 2\n    code: max\n")
    assert checksums(noisy) == base


def test_checksum_changes_on_price_and_limit_edits() -> None:
    base = checksums(TEXT)
    price = checksums(edit("prices: {month: 49900,", "prices: {month: 59900,"))
    assert price[("pro", None)] != base[("pro", None)]
    assert price[("max", None)] == base[("max", None)]
    limit = checksums(edit("      memory_facts: 500\n", "      memory_facts: 600\n"))
    assert limit[("pro", None)] != base[("pro", None)]
    assert limit[("free", None)] == base[("free", None)]
    seats = checksums(edit("min_seats: 50", "min_seats: 40"))
    assert seats[("org_pro", OrgVariant.COLLEGE)] != base[("org_pro", OrgVariant.COLLEGE)]
    groups = checksums(
        edit(
            "default_group: smart\n    prices: {month: 49900, year: 499000}\n"
            "    models: {groups: [fast, smart]}",
            "default_group: fast\n    prices: {month: 49900, year: 499000}\n"
            "    models: {groups: [fast]}",
        )
    )
    assert groups[("pro", None)] != base[("pro", None)]
    assert ModelGroup.FAST in parse(TEXT).plans[1].groups


def test_models_keep_picker_order_with_labels_and_providers() -> None:
    entries = parse(TEXT).model_entries()
    assert [e.sort_order for e in entries] == list(range(11))
    groups = [e.group.value for e in entries]
    assert groups == ["fast"] * 2 + ["smart"] * 5 + ["best"] * 2 + ["ultra"] * 2
    first = entries[0]
    assert (first.id, first.label, first.provider) == ("openai/gpt-oss-20b", "GPT-OSS 20B", "groq")
    assert entries[5].id == "gemini-3.1-flash-lite" and entries[5].provider == "google"


def test_default_groups_follow_the_catalog() -> None:
    by = {b.key: b for b in blocks(parse(TEXT))}
    assert by[("free", None)].default_group is ModelGroup.FAST
    for key in (("pro", None), ("max", None), ("payg", None)):
        assert by[key].default_group is ModelGroup.SMART
    assert by[("org_pro", OrgVariant.COLLEGE)].default_group is ModelGroup.SMART
    assert by[("org_max", OrgVariant.COMPANY)].default_group is ModelGroup.SMART


def test_default_group_joins_the_checksum_but_labels_do_not() -> None:
    base = checksums(TEXT)
    assert checksums(edit("label: GPT-OSS 20B,", "label: GPT-OSS Twenty,")) == base
    pro = "rank: 1\n    default_group: "
    moved = checksums(edit(pro + "smart", pro + "fast"))
    assert moved[("pro", None)] != base[("pro", None)]
    assert moved[("max", None)] == base[("max", None)]
