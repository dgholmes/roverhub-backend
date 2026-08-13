from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.commands import CommandStore
from control.router import build_router


class _FakeMqttClient:
    def __init__(self):
        self.published_commands = []

    def publish_command(self, robot_id, command):
        self.published_commands.append((robot_id, command))


def _make_app():
    store = CommandStore()
    mqtt_client = _FakeMqttClient()
    app = FastAPI()
    app.include_router(build_router(store, mqtt_client))
    return app, store, mqtt_client


def test_issue_command_accepts_wired_type_and_publishes():
    app, store, mqtt_client = _make_app()
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/commands",
        json={"type": "ESTOP", "label": "E-Stop", "initiated_by": "operator-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "ESTOP"
    assert body["current_stage"] == "requested"
    assert store.get(body["command_id"]) is not None
    assert len(mqtt_client.published_commands) == 1


def test_issue_command_rejects_unwired_type():
    # PAUSE, not DRIVE: DRIVE became wired on 2026-08-13 for continuous manual
    # control. PAUSE stays unwired because it depends on mission_executor, which
    # is still an empty placeholder.
    app, _, _ = _make_app()
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/commands",
        json={"type": "PAUSE", "label": "Pause", "initiated_by": "operator-1"},
    )

    assert response.status_code == 400


def test_issue_command_accepts_drive():
    """DRIVE carries a held velocity setpoint for continuous manual control --
    the bridge holds the resulting sequence. See
    robot_bridge/velocity_driver.py."""
    app, _, _ = _make_app()
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/commands",
        json={
            "type": "DRIVE",
            "label": "Drive",
            "initiated_by": "operator-1",
            "params": {"vx": 0.8, "vy": 0.0, "vyaw": 0.0},
        },
    )

    assert response.status_code == 200


def test_get_command_returns_stored_record():
    app, store, _ = _make_app()
    client = TestClient(app)
    post_response = client.post(
        "/robots/robot-dev-01/commands",
        json={"type": "ESTOP", "label": "E-Stop", "initiated_by": "operator-1"},
    )
    command_id = post_response.json()["command_id"]

    response = client.get(f"/robots/robot-dev-01/commands/{command_id}")

    assert response.status_code == 200
    assert response.json()["command_id"] == command_id


def test_get_command_404_for_unknown_id():
    app, _, _ = _make_app()
    client = TestClient(app)
    response = client.get("/robots/robot-dev-01/commands/does-not-exist")
    assert response.status_code == 404
