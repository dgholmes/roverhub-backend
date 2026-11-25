"""Switching AI detection from the console (SET_DETECTION, schema 0.0.31)
through POST /robots/{id}/commands: who may send it, its params, and the
bridge-version gate. It moves nothing on the robot -- the bridge writes the
setting to a file and perception restarts into it -- so it needs what
SET_OBSTACLE_AVOIDANCE needs, the other switch an operator flips mid-run."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.policy import command_capabilities  # noqa: E402
from control.router import DETECTION_SCHEMA_VERSION  # noqa: E402
from shared.schemas.commands import MISSION_SHARED_COMMAND_TYPES, WIRED_COMMAND_TYPES  # noqa: E402
from tests import test_site_setup as site_setup  # noqa: E402
from tests.test_site_setup import DEVICE_A, SITE, _bearer, _command  # noqa: E402

world = site_setup.world
auth_on = site_setup.auth_on

ON = {"enabled": True, "detectors": ["base.fall"]}


def test_it_is_wired_shared_with_missions_and_needs_what_obstacle_avoidance_needs():
    assert "SET_DETECTION" in WIRED_COMMAND_TYPES
    assert "SET_DETECTION" in MISSION_SHARED_COMMAND_TYPES
    assert command_capabilities("SET_DETECTION") == command_capabilities("SET_OBSTACLE_AVOIDANCE") == ("drive",)
    assert DETECTION_SCHEMA_VERSION == "0.0.31"


@pytest.mark.parametrize("params", [
    ON,
    {"enabled": False},
    {"enabled": True},                                          # all detectors
    {"enabled": True, "detectors": ["base.person", "base.pose"]},
])
def test_sign_in_off_sends_it_with_its_params(world, params):
    response = _command(TestClient(backend.app), "SET_DETECTION", params)
    assert response.status_code == 200, response.text
    sent, = world["sent"]
    assert sent.type == "SET_DETECTION" and sent.params == params


@pytest.mark.parametrize("user, allowed", [
    ("eve", True),      # engineer
    ("sue", True),      # supervisor
    ("ann", True),      # operator: flips it mid-run, like obstacle avoidance
    ("tom", False),     # technician: does not drive
    ("vic", False),     # viewer
])
def test_who_may_send_it(auth_on, world, user, allowed):
    client = TestClient(backend.app)
    response = _command(client, "SET_DETECTION", ON, headers=_bearer(client, user))
    assert response.status_code == (200 if allowed else 403), (user, response.text)
    obstacle = _command(client, "SET_OBSTACLE_AVOIDANCE", {"enabled": True}, headers=_bearer(client, user))
    assert obstacle.status_code == response.status_code, user


def test_a_device_key_cannot_send_it(auth_on, world):
    assert _command(TestClient(backend.app), "SET_DETECTION", ON, headers=DEVICE_A).status_code == 403


@pytest.mark.parametrize("params, fields", [
    ({}, "enabled"),
    (None, "enabled"),
    ({"enabled": "yes"}, "enabled"),                            # StrictBool: no strings
    ({"enabled": 1}, "enabled"),
    ({"enabled": True, "detectors": []}, "detectors"),          # on with nothing is off
    ({"enabled": True, "detectors": ["base.car"]}, "detectors.0"),
    ({"enabled": True, "detectors": ["base.pose", "base.pose"]}, "detectors"),
])
def test_unusable_params_are_a_422(world, params, fields):
    response = _command(TestClient(backend.app), "SET_DETECTION", params)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == f"SET_DETECTION params are not a detection setting ({fields})"
    assert world["sent"] == []


def test_a_bridge_older_than_0_0_31_is_refused_with_the_reason(world):
    backend.registry.upsert_from_registration("rover-030", SITE, "quad", schema_version="0.0.30")
    response = _command(TestClient(backend.app), "SET_DETECTION", ON, robot="rover-030")
    assert response.status_code == 409
    assert response.json()["detail"] == ("update the edge device: it runs schema 0.0.30, "
                                         "switching AI detection needs 0.0.31")
    assert world["sent"] == []


def test_a_bridge_that_never_said_its_version_is_refused_too(world):
    backend.registry.upsert_from_registration("rover-mute", SITE, "quad")
    response = _command(TestClient(backend.app), "SET_DETECTION", ON, robot="rover-mute")
    assert response.status_code == 409
    assert response.json()["detail"] == ("update the edge device: it runs schema unknown, "
                                         "switching AI detection needs 0.0.31")
    assert world["sent"] == []
