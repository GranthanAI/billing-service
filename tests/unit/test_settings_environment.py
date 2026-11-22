import pytest
from pydantic import ValidationError

from billing.settings import (
    Settings,
    check_email_config,
    check_gateway_allowed,
    check_simulation_allowed,
    normalise_environment,
)


def settings(**kw: str) -> Settings:
    base = dict(
        billing_database_url="x", billing_valkey_url="x",
        kafka_bootstrap_servers="x", razorpay_key_id="k", razorpay_key_secret="s",
        razorpay_webhook_secret="primary", service_token_keys="t", supplier_state_code="10",
        gateway_shared_secret="g",
    )  # fmt: skip
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["prod", "Production", " production ", "PROD"])
def test_production_spellings_normalise(raw: str) -> None:
    assert normalise_environment(raw) == "production"
    assert settings(environment=raw).environment == "production"


@pytest.mark.parametrize("raw", ["qa", "", "dev", "  "])
def test_unknown_environment_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalise_environment(raw)
    with pytest.raises(ValidationError):
        settings(environment=raw)


@pytest.mark.parametrize("raw", ["Staging", "test", " development"])
def test_other_known_names_pass(raw: str) -> None:
    assert settings(environment=raw).environment == raw.strip().lower()


@pytest.mark.parametrize("env", ["prod", "Production", " production "])
def test_guards_trigger_on_raw_strings(env: str) -> None:
    with pytest.raises(ValueError):
        check_simulation_allowed(True, env)
    with pytest.raises(ValueError):
        check_gateway_allowed("fake", env)
    for mode in ("console", "file"):
        with pytest.raises(ValueError):
            check_email_config(mode, env, "", "")


@pytest.mark.parametrize("env", ["prod", "Production", " production "])
def test_production_needs_the_org_service_url_and_token(env: str) -> None:
    from billing.settings import check_org_service_config

    check_org_service_config("http://org:8010", "t" * 32, env)
    for url, token, missing in [("", "t", "ORG_SERVICE_URL"), ("http://o", "", "ORG_SERVICE_TOKEN"), ("  ", "  ", "ORG_SERVICE_URL")]:
        with pytest.raises(ValueError, match=missing):
            check_org_service_config(url, token, env)


@pytest.mark.parametrize("env", ["development", "test", "staging"])
def test_the_org_service_may_be_off_outside_production(env: str) -> None:
    from billing.settings import check_org_service_config

    check_org_service_config("", "", env)
