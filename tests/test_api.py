import pytest

from api.app import app, registry


@pytest.fixture(autouse=True)
def _clear_registry():
    registry._robots.clear()
    yield


class _Subscriber:
    def subscribe(self, topic, qos=0):
        pass


def test_healthz_returns_ok(monkeypatch):
    from fastapi.testclient import TestClient

    import api.app as backend

    monkeypatch.setattr(backend.mqtt_client, "_connected", True)
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["broker"]["connected"] is True


def test_healthz_reports_a_dead_broker_link_without_failing(monkeypatch):
    """A dead link is visible on the public health check -- and still a 200,
    so the edge's clock sync (which reads server_time_ms) keeps working."""
    from fastapi.testclient import TestClient

    import api.app as backend

    client = backend.mqtt_client
    for name in ("_connected", "_connected_at", "_connects", "_disconnects", "_last_disconnect_at",
                 "_last_disconnect_rc", "_last_refused_rc", "_last_message_at"):
        monkeypatch.setattr(client, name, getattr(client, name))
    times = iter([1000.0, 1001.0, 1010.0, 1030.0])
    monkeypatch.setattr(client, "_clock", lambda: next(times))
    client._on_connect(_Subscriber(), None, None, 0)      # 1000: up
    client._on_message(None, None, type("M", (), {"topic": "x", "payload": b"{}"})())  # 1001: a message
    client._on_disconnect(None, None, 7)                  # 1010: dropped
    client._on_connect(_Subscriber(), None, None, 5)      # refused, not authorised
    body = TestClient(app).get("/healthz")
    assert body.status_code == 200
    payload = body.json()
    assert payload["status"] == "degraded"
    broker = payload["broker"]
    assert broker["connected"] is False
    assert broker["last_message_age_s"] == 29.0           # read at 1030
    assert broker["disconnect_count"] == 1 and broker["last_disconnect_rc"] == 7
    assert broker["last_refused_rc"] == 5
    assert broker["reconnect_count"] == 0
    assert isinstance(payload["server_time_ms"], int)


def test_healthz_carries_the_server_time_for_edge_clock_sync():
    """Edge bridges anchor their timestamps to this (robot_bridge/clock.py):
    the Jetson has no RTC battery and boots with a stale clock."""
    import time

    from fastapi.testclient import TestClient

    before = time.time() * 1000
    body = TestClient(app).get("/healthz").json()
    after = time.time() * 1000
    assert isinstance(body["server_time_ms"], int)
    assert before - 1 <= body["server_time_ms"] <= after + 1


def test_robots_endpoint_lists_registered_robots():
    from fastapi.testclient import TestClient

    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client = TestClient(app)
    response = client.get("/robots")
    assert response.status_code == 200
    body = response.json()
    assert any(r["robot_id"] == "robot-dev-01" and r["robot_type"] == "quad" for r in body)


# --- Device acceptance and host info in GET /robots ------------------------

from fastapi.testclient import TestClient  # noqa: E402


def test_robots_listing_exposes_acceptance_and_host_fields():
    registry.upsert_from_registration(
        "robot-dev-01", "site-dev", "quad", bridge_id="bridge-dev-01",
        schema_version="0.0.9", host={"hostname": "jetson"},
    )
    registry.update_host_metrics("robot-dev-01", {"cpu_percent": 3.0}, at="t")

    robot = TestClient(app).get("/robots").json()[0]

    assert robot["acceptance"] == "accepted"  # default mode is auto
    assert robot["acceptance_source"] == "auto"
    assert robot["bridge_id"] == "bridge-dev-01"
    assert robot["schema_version"] == "0.0.9"
    assert robot["host"] == {"hostname": "jetson"}
    assert robot["host_metrics"] == {"cpu_percent": 3.0}
    assert robot["host_metrics_at"] == "t"
    assert robot["first_seen"] is not None


def _manual(monkeypatch):
    monkeypatch.setattr(registry, "_mode", "manual")
    registry.upsert_from_registration("robot-new", "site-dev", "wheel")


def test_pending_lists_only_undecided_devices(monkeypatch):
    registry.upsert_from_registration("robot-known", "site-dev", "quad")
    _manual(monkeypatch)
    client = TestClient(app)

    assert [r["robot_id"] for r in client.get("/robots/pending").json()] == ["robot-new"]
    filtered = client.get("/robots", params={"acceptance": "pending"}).json()
    assert [r["robot_id"] for r in filtered] == ["robot-new"]
    assert client.get("/robots", params={"acceptance": "bogus"}).status_code == 422


def test_accept_and_reject_record_an_operator_decision(monkeypatch):
    _manual(monkeypatch)
    client = TestClient(app)

    accepted = client.post("/robots/robot-new/accept").json()
    assert (accepted["acceptance"], accepted["acceptance_source"]) == ("accepted", "operator")
    assert client.get("/robots/pending").json() == []

    rejected = client.post("/robots/robot-new/reject").json()
    assert rejected["acceptance"] == "rejected"


def test_acceptance_endpoints_404_on_unknown_robots():
    client = TestClient(app)
    assert client.post("/robots/ghost/accept").status_code == 404
    assert client.post("/robots/ghost/reject").status_code == 404
    assert client.delete("/robots/ghost").status_code == 404


def test_forget_removes_the_robot():
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client = TestClient(app)

    assert client.delete("/robots/robot-dev-01").json() == {"robot_id": "robot-dev-01", "forgotten": True}
    assert client.get("/robots").json() == []


def test_a_rejected_robot_can_still_be_commanded(monkeypatch):
    """Acceptance is informational. If this ever starts failing because someone
    made it a gate, read robots/registry.py first: without auth it secures
    nothing, and a backend restart would strand every robot."""
    from api.app import mqtt_client

    _manual(monkeypatch)
    client = TestClient(app)
    client.post("/robots/robot-new/reject")
    published = []
    monkeypatch.setattr(
        mqtt_client, "publish_command", lambda robot_id, command: published.append(robot_id),
    )

    response = client.post("/robots/robot-new/commands", json={
        "type": "ESTOP", "label": "E-Stop", "initiated_by": "operator-dev",
    })

    assert response.status_code == 200, response.text
    assert published == ["robot-new"]


def test_robots_listing_exposes_nav_goal_or_null():
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    assert TestClient(app).get("/robots").json()[0]["nav_goal"] is None

    goal = {"goal_id": "c-1", "x_m": 1.0, "y_m": 2.0, "yaw_rad": None, "map_id": "m",
            "state": "active", "reason": None, "updated_at": "t"}
    registry.update_state("robot-dev-01", "STAND", "balance_stand", nav_goal=goal)

    assert TestClient(app).get("/robots").json()[0]["nav_goal"] == goal


def test_the_app_refuses_a_command_for_a_robot_it_does_not_know(monkeypatch):
    from api.app import mqtt_client

    published = []
    monkeypatch.setattr(mqtt_client._client, "publish", lambda *a, **k: published.append(a))

    response = TestClient(app).post("/robots/ghost/commands", json={
        "type": "ESTOP", "label": "E-Stop", "initiated_by": "operator-dev"})

    assert response.status_code == 404
    assert response.json() == {"detail": "robot not found"}
    assert published == []
