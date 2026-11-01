from fastapi.testclient import TestClient

from billing.main_api import app


def test_healthz() -> None:
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200
    assert r.headers["X-Correlation-ID"]
