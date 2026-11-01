from pathlib import Path

import pytest

from billing.cli import main
from billing.settings import check_gateway_allowed

ROOT = Path(__file__).resolve().parents[2]


def test_invalid_catalog_exits_nonzero_before_touching_the_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BILLING_MIGRATION_DATABASE_URL", raising=False)  # would fail if reached
    bad = tmp_path / "plans.yaml"
    bad.write_text((ROOT / "config/plans.yaml").read_text().replace("  custom_modes: 3\n", ""))
    assert main(["catalog", "sync", "--file", str(bad)]) == 1
    assert "custom_modes" in capsys.readouterr().err


def test_fake_gateway_is_refused_in_production(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BILLING_MIGRATION_DATABASE_URL", "postgresql+asyncpg://x@nowhere/x")
    monkeypatch.setenv("PAYMENT_GATEWAY", "fake")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert main(["catalog", "sync", "--file", str(ROOT / "config/plans.yaml")]) == 1
    assert "refused" in capsys.readouterr().err
    with pytest.raises(ValueError):
        check_gateway_allowed("fake", "production")
    check_gateway_allowed("fake", "development")
    check_gateway_allowed("razorpay", "production")


def test_razorpay_adapter_is_not_built_yet(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BILLING_MIGRATION_DATABASE_URL", "postgresql+asyncpg://x@nowhere/x")
    monkeypatch.setenv("PAYMENT_GATEWAY", "razorpay")
    assert main(["catalog", "sync", "--file", str(ROOT / "config/plans.yaml")]) == 1
    assert "Phase 6" in capsys.readouterr().err
