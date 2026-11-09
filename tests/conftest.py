import asyncio
import os
import sys
import uuid

from hypothesis import HealthCheck, settings

# One id per test run. The controller sets it before it starts the xdist workers, which inherit
# it; every shared-service resource name (databases, Valkey db index) includes it.
RUN_ID = os.environ.setdefault("TEST_RUN_ID", uuid.uuid4().hex[:5])

settings.register_profile("ci", max_examples=500, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile(
    "nightly", max_examples=10_000, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "fast", max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

if sys.platform == "win32" and os.environ.get("TEST_SELECTOR_LOOP") == "1":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


KAFKA_FIXTURES = {"kafka", "producer"}
SIM_FIXTURES = {"sim_env", "sim_lifecycle_env", "sim_http"}


def pytest_collection_modifyitems(items):  # type: ignore[no-untyped-def]
    """Kafka tests share one xdist worker (--dist loadgroup). Simulator tests are marked `sim`."""
    import pytest

    for item in items:
        fixtures = set(getattr(item, "fixturenames", ()))
        path = item.nodeid.split("::")[0]
        if fixtures & KAFKA_FIXTURES or "kafka" in path:
            item.add_marker(pytest.mark.kafka)
            item.add_marker(pytest.mark.xdist_group("serial"))
        if fixtures & SIM_FIXTURES or "test_sim_" in path or "lifecycle_sim" in path:
            item.add_marker(pytest.mark.sim)
