import pytest

from api.app import app, registry


@pytest.fixture(autouse=True)
def _clear_registry():
    registry._robots.clear()
    yield


def test_healthz_returns_ok():
    from fastapi.testclient import TestClient

    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_robots_endpoint_lists_registered_robots():
    from fastapi.testclient import TestClient

    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client = TestClient(app)
    response = client.get("/robots")
    assert response.status_code == 200
    body = response.json()
    assert any(r["robot_id"] == "robot-dev-01" and r["robot_type"] == "quad" for r in body)
