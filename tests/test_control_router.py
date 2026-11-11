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


def test_start_mission_is_refused_here_so_it_cannot_bypass_the_dispatcher():
    app, store, mqtt_client = _make_app()
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/commands",
        json={"type": "START_MISSION", "label": "Start", "initiated_by": "x",
              "params": {"run_id": "r-1", "steps": []}},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "use POST /robots/robot-dev-01/runs to start a mission"}
    assert mqtt_client.published_commands == []


def test_stop_mission_is_still_allowed_here():
    app, store, mqtt_client = _make_app()
    response = TestClient(app).post(
        "/robots/robot-dev-01/commands",
        json={"type": "STOP_MISSION", "label": "Stop", "initiated_by": "x"})
    assert response.status_code == 200
    assert len(mqtt_client.published_commands) == 1


_NAV_GOAL = {"x_m": 3.5, "y_m": -1.25, "yaw_rad": None, "map_id": "map-venue", "floor_id": "f1"}


def test_issue_command_accepts_nav_goto_and_publishes_its_params_unchanged():
    app, _, mqtt_client = _make_app()
    response = TestClient(app).post(
        "/robots/robot-dev-01/commands",
        json={"type": "NAV_GOTO", "label": "Go to point", "initiated_by": "operator-1",
              "params": _NAV_GOAL})

    assert response.status_code == 200
    assert response.json()["type"] == "NAV_GOTO"
    (_, command), = mqtt_client.published_commands
    assert command.params == _NAV_GOAL


def test_nav_goto_with_a_goal_the_bridge_could_not_use_is_refused_before_publish():
    app, _, mqtt_client = _make_app()
    client = TestClient(app)
    for params in (None, {"x_m": 1.0, "y_m": 2.0}, {**_NAV_GOAL, "x_m": "far"},
                   {**_NAV_GOAL, "floor_id": None}):
        response = client.post(
            "/robots/robot-dev-01/commands",
            json={"type": "NAV_GOTO", "label": "Go", "initiated_by": "x", "params": params})
        assert response.status_code == 422, params
        # Refused by our own check, not by CommandRequest's type literal.
        assert "NAV_GOTO params are not a usable goal" in response.json()["detail"], params
    assert mqtt_client.published_commands == []


def test_issue_command_accepts_nav_cancel():
    app, _, mqtt_client = _make_app()
    response = TestClient(app).post(
        "/robots/robot-dev-01/commands",
        json={"type": "NAV_CANCEL", "label": "Cancel goal", "initiated_by": "operator-1"})

    assert response.status_code == 200
    assert len(mqtt_client.published_commands) == 1


_POSE = {"x_m": 3.5, "y_m": -1.25, "heading_deg": 90.0, "map_id": "map-venue", "floor_id": "f1"}


def test_issue_command_accepts_reset_position_and_publishes_its_params_unchanged():
    app, _, mqtt_client = _make_app()
    response = TestClient(app).post(
        "/robots/robot-dev-01/commands",
        json={"type": "RESET_POSITION", "label": "Set position", "initiated_by": "operator-1",
              "params": _POSE})

    assert response.status_code == 200
    assert response.json()["type"] == "RESET_POSITION"
    (_, command), = mqtt_client.published_commands
    assert command.params == _POSE


def test_reset_position_with_a_pose_the_bridge_could_not_use_is_refused_before_publish():
    app, _, mqtt_client = _make_app()
    client = TestClient(app)
    for params in (None, {"x_m": 1.0, "y_m": 2.0, "map_id": "m", "floor_id": "f"},
                   {**_POSE, "heading_deg": "north"}, {**_POSE, "x_m": None},
                   {**_POSE, "map_id": None}):
        response = client.post(
            "/robots/robot-dev-01/commands",
            json={"type": "RESET_POSITION", "label": "Set", "initiated_by": "x", "params": params})
        assert response.status_code == 422, params
        assert "RESET_POSITION params are not a usable pose" in response.json()["detail"], params
    assert mqtt_client.published_commands == []


# -- a robot the registry does not know (audit 2026-11-10) ---------------------

def _make_app_with_registry():
    from robots.registry import RobotRegistry

    registry = RobotRegistry()
    store = CommandStore()
    mqtt_client = _FakeMqttClient()
    app = FastAPI()
    app.include_router(build_router(store, mqtt_client, registry=registry))
    return TestClient(app), registry, store, mqtt_client


def test_a_command_for_an_unknown_robot_is_404_and_nothing_is_published_or_stored():
    client, _, store, mqtt_client = _make_app_with_registry()

    response = client.post(
        "/robots/ghost/commands", json={"type": "ESTOP", "label": "E-Stop", "initiated_by": "x"})

    assert response.status_code == 404
    assert response.json() == {"detail": "robot not found"}
    assert mqtt_client.published_commands == []
    assert store._commands == {}


def test_a_known_robot_that_is_offline_is_still_sent_the_command():
    client, registry, _, mqtt_client = _make_app_with_registry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    registry.mark_offline("robot-dev-01")

    response = client.post(
        "/robots/robot-dev-01/commands", json={"type": "ESTOP", "label": "E-Stop", "initiated_by": "x"})

    assert response.status_code == 200
    assert [robot_id for robot_id, _ in mqtt_client.published_commands] == ["robot-dev-01"]


def test_a_robot_forgotten_between_the_check_and_the_publish_is_404_not_sent():
    # The client is the last line: it refuses rather than build a topic for "unknown".
    from realtime.mqtt_client import UnknownRobotError

    class _Forgets(_FakeMqttClient):
        def publish_command(self, robot_id, command):
            raise UnknownRobotError("robot not found")

    store = CommandStore()
    app = FastAPI()
    app.include_router(build_router(store, _Forgets()))
    response = TestClient(app).post(
        "/robots/ghost/commands", json={"type": "ESTOP", "label": "E-Stop", "initiated_by": "x"})

    assert response.status_code == 404
    assert response.json() == {"detail": "robot not found"}
    (command,) = store._commands.values()
    assert command.current_stage == "execution_failed"


# -- command time limits and RESUME (schema 0.0.16) ----------------------------

def test_a_published_command_carries_its_time_limit():
    from shared.schemas.commands import command_ttl_s

    client, registry, _, mqtt_client = _make_app_with_registry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    for type_ in ("SET_STATE", "DRIVE", "SNAPSHOT"):
        client.post("/robots/robot-dev-01/commands", json={"type": type_, "label": type_, "initiated_by": "x"})
    sent = {command.type: command.ttl_s for _, command in mqtt_client.published_commands}
    assert sent == {t: command_ttl_s(t) for t in ("SET_STATE", "DRIVE", "SNAPSHOT")}
    assert all(v is not None and v > 0 for v in sent.values())


def test_a_stop_is_published_with_no_time_limit():
    from shared.schemas.commands import COMMAND_TTL_EXEMPT

    client, registry, _, mqtt_client = _make_app_with_registry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    for type_ in ("ESTOP", "STOP_MISSION", "NAV_CANCEL", "RELEASE_CONTROL"):
        assert type_ in COMMAND_TTL_EXEMPT
        client.post("/robots/robot-dev-01/commands", json={"type": type_, "label": type_, "initiated_by": "x"})
    assert [command.ttl_s for _, command in mqtt_client.published_commands] == [None] * 4


def test_a_release_and_resume_carries_a_time_limit_and_a_plain_release_none():
    # Release & resume sets a paused mission walking again (schema 0.0.19).
    from shared.schemas.commands import COMMAND_TTL_RESUMING_RELEASE_S

    client, registry, _, mqtt_client = _make_app_with_registry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    for params in ({"resume_run": True}, {}):
        client.post("/robots/robot-dev-01/commands",
                    json={"type": "RELEASE_CONTROL", "label": "Release", "initiated_by": "x", "params": params})
    assert [command.ttl_s for _, command in mqtt_client.published_commands] == [
        COMMAND_TTL_RESUMING_RELEASE_S, None]


def test_a_walk_is_published_with_a_few_seconds_to_arrive():
    client, registry, _, mqtt_client = _make_app_with_registry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client.post("/robots/robot-dev-01/commands",
                json={"type": "LINE_WALK", "label": "Walk", "initiated_by": "x", "params": {"distance_m": 1.0}})
    (_, command), = mqtt_client.published_commands
    assert command.ttl_s == 5.0
    # The issue time the bridge measures the age from is the backend's stamp.
    assert command.stages[0].stage == "requested"


def test_resume_needs_the_run_id_of_the_paused_run():
    client, registry, _, mqtt_client = _make_app_with_registry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    for params in (None, {}, {"run_id": ""}, {"run_id": 7}):
        response = client.post("/robots/robot-dev-01/commands",
                               json={"type": "RESUME", "label": "Retry", "initiated_by": "x", "params": params})
        assert response.status_code == 422, params
        assert response.json()["detail"] == "RESUME needs the run_id of the run to retry", params
    assert mqtt_client.published_commands == []

    response = client.post("/robots/robot-dev-01/commands",
                           json={"type": "RESUME", "label": "Retry", "initiated_by": "x",
                                 "params": {"run_id": "run-1"}})
    assert response.status_code == 200
    (_, command), = mqtt_client.published_commands
    assert command.params == {"run_id": "run-1"} and command.ttl_s == 10.0
