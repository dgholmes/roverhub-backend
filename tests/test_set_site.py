"""Moving a robot to another site (SET_SITE, schema 0.0.29): the broker side.

The command's own checks (an unknown site is a 404, `configure` is needed at
both sites, an old bridge is refused) are in test_map_ready_commands.py. This
file is the broker's: a device key writes only under the site its robot
registered at (auth/mqtt_hooks.py), and a moved bridge comes back and writes
under the NEW site -- its online heartbeat goes out before its registration
does, and mosquitto-go-auth caches a refusal for 30 s. So once the backend has
sent a SET_SITE it lets that robot's key write at the site it is moving to as
well (robots/site_moves.py), until the robot is heard from there, refuses the
move, or the allowance runs out.

There is one organisation per backend (auth/users.py), so every site this
backend knows is the organisation's: there is no other boundary to check."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.mqtt_hooks import ACC_READ, ACC_SUBSCRIBE, ACC_WRITE  # noqa: E402
from robots.site_moves import SITE_MOVE_TTL_S, SiteMoves  # noqa: E402
from shared.schemas.commands import Command, CommandStageEvent  # noqa: E402
from tests import test_site_setup as site_setup  # noqa: E402
from tests.test_site_setup import OTHER_SITE, SITE, _bearer, _command  # noqa: E402

world = site_setup.world
auth_on = site_setup.auth_on

THIRD_SITE = "site-third"


def _register(robot_id="rover-a", site=SITE, schema="0.0.29"):
    payload = {"bridge_id": "bridge-%s" % robot_id, "site_id": site, "robot_id": robot_id,
               "robot_type": "quad", "schema_version": schema}
    backend.mqtt_client.handle_message("roverhub/admin/bridge-%s/register" % robot_id, json.dumps(payload).encode())


def _acl(client, username, topic, acc):
    return client.post("/mqtt/acl", json={"username": username, "clientid": "c", "topic": topic,
                                          "acc": acc}).status_code


def _topic(site, suffix, robot="rover-a"):
    return "roverhub/%s/%s/%s" % (site, robot, suffix)


def _ack(command: Command, stage: str, **extra) -> None:
    acked = command.model_copy(update={"current_stage": stage, **extra})
    acked.stages = list(command.stages) + [CommandStageEvent(stage=stage, at="2026-11-23T00:00:00+00:00")]
    backend.mqtt_client.handle_message(_topic(SITE, "ack"), acked.model_dump_json().encode())


@pytest.fixture
def moving(auth_on):
    """rover-a at SITE on 0.0.29; `both` engineers SITE and OTHER_SITE. The
    app's SiteMoves, emptied before and after."""
    _register()
    auth_on.users.create(username="both", display_name="both", password=site_setup.PASSWORD, org_role="member",
                         site_roles={SITE: "engineer", OTHER_SITE: "engineer"})
    backend.site_moves.clear()
    yield backend.site_moves
    backend.site_moves.clear()


def _move(client, site=OTHER_SITE):
    response = _command(client, "SET_SITE", {"site_id": site}, headers=_bearer(client, "both"))
    assert response.status_code == 200, response.text
    return Command(**response.json())


def test_device_topics_allowed_at_the_new_site(moving):
    client = TestClient(backend.app)
    # Before the move: its key writes at its own site only.
    assert _acl(client, "rover-a", _topic(SITE, "heartbeat"), ACC_WRITE) == 200
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "heartbeat"), ACC_WRITE) == 403
    _move(client)
    # Sent: the bridge restarts under OTHER_SITE, and its first writes there --
    # the online heartbeat, then state -- go out before its registration.
    for suffix in ("heartbeat", "state", "telemetry", "ack", "survey/status", "sensors/lidar_mount"):
        assert _acl(client, "rover-a", _topic(OTHER_SITE, suffix), ACC_WRITE) == 200, suffix
    # The ack and the goodbye still go out under the old site.
    assert _acl(client, "rover-a", _topic(SITE, "ack"), ACC_WRITE) == 200
    # Nowhere else, and not for another robot's key.
    assert _acl(client, "rover-a", _topic(THIRD_SITE, "heartbeat"), ACC_WRITE) == 403
    assert _acl(client, "rover-b", _topic(SITE, "heartbeat", robot="rover-b"), ACC_WRITE) == 403
    assert _acl(client, "rover-b", _topic(OTHER_SITE, "heartbeat", robot="rover-a"), ACC_WRITE) == 403
    # Commands stay the backend's to write, wherever the robot is.
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "commands"), ACC_WRITE) == 403
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "commands"), ACC_SUBSCRIBE) == 200


def test_once_registered_at_the_new_site_the_old_one_is_closed(moving):
    client = TestClient(backend.app)
    _move(client)
    _register(site=OTHER_SITE)
    assert backend.registry.get("rover-a").site_id == OTHER_SITE
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "state"), ACC_WRITE) == 200
    assert _acl(client, "rover-a", _topic(SITE, "state"), ACC_WRITE) == 403
    assert moving.target("rover-a") is None


@pytest.mark.parametrize("stage", ["execution_failed", "timed_out", "cancelled"])
def test_a_refused_move_withdraws_the_new_site(moving, stage):
    client = TestClient(backend.app)
    command = _move(client)
    _ack(command, stage, failure_reason="REJECTED_NAV_GOAL_ACTIVE: the robot is walking to a point")
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "state"), ACC_WRITE) == 403
    assert _acl(client, "rover-a", _topic(SITE, "state"), ACC_WRITE) == 200


def test_the_restarting_ack_keeps_the_new_site_open(moving):
    client = TestClient(backend.app)
    command = _move(client)
    _ack(command, "execution_completed", result={"restarting": True})
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "state"), ACC_WRITE) == 200


def test_a_move_that_never_reached_the_broker_opens_nothing(moving, monkeypatch):
    from realtime.mqtt_client import CommandPublishError

    def _down(robot_id, command):
        raise CommandPublishError("the broker is not connected")

    monkeypatch.setattr(backend.mqtt_client, "publish_command", _down)
    client = TestClient(backend.app)
    response = _command(client, "SET_SITE", {"site_id": OTHER_SITE}, headers=_bearer(client, "both"))
    assert response.status_code == 503
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "state"), ACC_WRITE) == 403


def test_a_refused_request_opens_nothing(moving):
    client = TestClient(backend.app)
    # eve engineers SITE only: refused at the target, nothing sent.
    response = _command(client, "SET_SITE", {"site_id": OTHER_SITE}, headers=_bearer(client, "eve"))
    assert response.status_code == 403
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "state"), ACC_WRITE) == 403


def test_reads_are_unchanged_by_a_move(moving):
    # Browsers read by their roles; a device subscribes to its commands.
    client = TestClient(backend.app)
    _move(client)
    assert _acl(client, "rover-a", _topic(OTHER_SITE, "logs/request"), ACC_READ) == 200
    assert _acl(client, "rover-a", _topic(THIRD_SITE, "commands"), ACC_WRITE) == 403


# --- SiteMoves on its own -----------------------------------------------------


def test_site_moves_run_out():
    now = [1000.0]
    moves = SiteMoves(clock=lambda: now[0])
    moves.begin("rover-a", OTHER_SITE, "c1")
    assert moves.target("rover-a") == OTHER_SITE
    now[0] += SITE_MOVE_TTL_S + 1
    assert moves.target("rover-a") is None


def _set_site(command_id, site, stage):
    return Command(command_id=command_id, robot_id="rover-a", type="SET_SITE", label="", params={"site_id": site},
                   current_stage=stage, initiated_by="both")


def test_only_the_moves_own_command_ends_it():
    moves = SiteMoves()
    moves.begin("rover-a", OTHER_SITE, "c2")
    moves.settle(_set_site("c1", THIRD_SITE, "execution_failed"))
    assert moves.target("rover-a") == OTHER_SITE
    moves.settle(_set_site("c2", OTHER_SITE, "execution_completed"))
    assert moves.target("rover-a") == OTHER_SITE
    moves.settle(_set_site("c2", OTHER_SITE, "execution_failed"))
    assert moves.target("rover-a") is None


def test_a_newer_move_replaces_the_older():
    moves = SiteMoves()
    moves.begin("rover-a", OTHER_SITE, "c1")
    moves.begin("rover-a", THIRD_SITE, "c2")
    assert moves.target("rover-a") == THIRD_SITE
