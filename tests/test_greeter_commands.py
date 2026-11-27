"""The Greet button and Greeter mode (GREET, SET_GREETER, schema 0.0.36)
through POST /robots/{id}/commands: who may send each, their params, the
bridge-version gate, and the audit rows named for them. Whether the robot may
greet now (standing, no run, battery, control) is the bridge's to say."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.policy import command_capabilities  # noqa: E402
from control.router import GREETER_SCHEMA_VERSION  # noqa: E402
from history.audit import AuditRecorder, bind_actor, reset_actor  # noqa: E402
from shared.schemas.commands import (  # noqa: E402
    COMMAND_TTL_S, MISSION_SHARED_COMMAND_TYPES, WIRED_COMMAND_TYPES, Command, CommandType,
)
from tests import test_site_setup as site_setup  # noqa: E402
from tests.test_site_setup import DEVICE_A, SITE, _bearer, _command  # noqa: E402

world = site_setup.world
auth_on = site_setup.auth_on

CLIP = "rover-a-0f8e2a4c-1b2d-4e5f-8a9b-0c1d2e3f4a5b.wav"
GREET = {"clip_id": CLIP, "dance": "groove", "wave_seconds": 3.0, "volume": 80}
SETTING = {"enabled": True, "clip_id": CLIP, "dance": "groove", "trigger_distance_m": 3.0,
           "facing_distance_m": 4.0, "dance_min_distance_m": 1.5, "cooldown_s": 300.0, "min_battery_pct": 30}
BODIES = {"GREET": GREET, "SET_GREETER": SETTING}


def test_both_are_wired_with_the_spec_capabilities():
    from typing import get_args
    assert {"GREET", "SET_GREETER"} <= WIRED_COMMAND_TYPES
    assert len(get_args(CommandType)) == 52
    assert len(WIRED_COMMAND_TYPES) == 48
    assert command_capabilities("GREET") == ("drive",)
    assert command_capabilities("SET_GREETER") == ("dispatchPatrols",)
    assert "SET_GREETER" in MISSION_SHARED_COMMAND_TYPES and "GREET" not in MISSION_SHARED_COMMAND_TYPES
    assert GREETER_SCHEMA_VERSION == "0.0.36"


@pytest.mark.parametrize("type_, params", [
    ("GREET", GREET),
    ("GREET", {}),                                             # wave only
    ("GREET", None),
    ("GREET", {"dance": None, "wave_seconds": 10}),
    ("SET_GREETER", SETTING),
    ("SET_GREETER", {"enabled": False}),
    ("SET_GREETER", {"enabled": True, "dance": None, "trigger_distance_m": 6, "facing_distance_m": 1}),
])
def test_sign_in_off_sends_it_with_its_params(world, type_, params):
    response = _command(TestClient(backend.app), type_, params)
    assert response.status_code == 200, response.text
    sent, = world["sent"]
    assert sent.type == type_ and sent.params == params


def test_a_greeting_expires_like_a_gesture(world):
    response = _command(TestClient(backend.app), "GREET", GREET)
    assert response.json()["ttl_s"] == COMMAND_TTL_S["GREET"] == 10.0


@pytest.mark.parametrize("user, greet, setting", [
    ("eve", True, True),      # engineer: every capability since 2026-11-27
    ("sue", True, True),      # supervisor: dispatchPatrols
    ("ann", True, False),     # operator: may press Greet, may not turn Greeter mode on
    ("tom", False, False),    # technician: does not drive
    ("vic", False, False),    # viewer
])
def test_who_may_send_each(auth_on, world, user, greet, setting):
    client = TestClient(backend.app)
    headers = _bearer(client, user)
    response = _command(client, "GREET", GREET, headers=headers)
    assert response.status_code == (200 if greet else 403), (user, response.text)
    response = _command(client, "SET_GREETER", SETTING, headers=headers)
    assert response.status_code == (200 if setting else 403), (user, response.text)
    if greet and not setting:   # past the route's own rule, refused by the command's
        assert response.json()["detail"] == "your role does not allow SET_GREETER"


@pytest.mark.parametrize("type_", ["GREET", "SET_GREETER"])
def test_a_device_key_cannot_send_it(auth_on, world, type_):
    assert _command(TestClient(backend.app), type_, BODIES[type_], headers=DEVICE_A).status_code == 403


@pytest.mark.parametrize("params, fields", [
    ({"wave_seconds": 0.5}, "wave_seconds"),
    ({"wave_seconds": 11}, "wave_seconds"),
    ({"volume": 101}, "volume"),
    ({"dance": "dance0"}, "dance"),                            # 126 s: not a greeting
    ({"dance": "twirl_jump"}, "dance"),                        # airborne
    ({"clip_id": "../../etc/passwd"}, "clip_id"),
    ({"wave_seconds": 0, "volume": -1}, "wave_seconds, volume"),
])
def test_an_unusable_greet_is_a_422_naming_the_fields(world, params, fields):
    response = _command(TestClient(backend.app), "GREET", params)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == f"GREET params are not a greeting ({fields})"
    assert world["sent"] == []


@pytest.mark.parametrize("params, fields", [
    ({}, "enabled"),
    (None, "enabled"),
    ({"enabled": "yes"}, "enabled"),                           # StrictBool
    ({"enabled": True, "trigger_distance_m": 0.5}, "trigger_distance_m"),
    ({"enabled": True, "facing_distance_m": 7}, "facing_distance_m"),
    ({"enabled": True, "dance_min_distance_m": 0.1}, "dance_min_distance_m"),
    ({"enabled": True, "cooldown_s": 10}, "cooldown_s"),
    ({"enabled": True, "min_battery_pct": 5}, "min_battery_pct"),
    ({"enabled": True, "dance": "wave"}, "dance"),
])
def test_an_unusable_setting_is_a_422_naming_the_fields(world, params, fields):
    response = _command(TestClient(backend.app), "SET_GREETER", params)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == f"SET_GREETER params are not a Greeter setting ({fields})"
    assert world["sent"] == []


@pytest.mark.parametrize("type_, what", [("GREET", "greeting"), ("SET_GREETER", "Greeter mode")])
def test_a_bridge_older_than_0_0_36_is_refused_with_the_reason(world, type_, what):
    backend.registry.upsert_from_registration("rover-035", SITE, "quad", schema_version="0.0.35")
    response = _command(TestClient(backend.app), type_, BODIES[type_], robot="rover-035")
    assert response.status_code == 409
    assert response.json()["detail"] == f"update the edge device: it runs schema 0.0.35, {what} needs 0.0.36"
    assert world["sent"] == []


@pytest.mark.parametrize("type_", ["GREET", "SET_GREETER"])
def test_a_bridge_that_never_said_its_version_is_refused_too(world, type_):
    backend.registry.upsert_from_registration("rover-mute", SITE, "quad")
    response = _command(TestClient(backend.app), type_, BODIES[type_], robot="rover-mute")
    assert response.status_code == 409
    assert "it runs schema unknown" in response.json()["detail"]
    assert world["sent"] == []


# --- the audit rows ------------------------------------------------------------------

def _published(type_, params, error=None):
    rows = []
    audit = AuditRecorder(rows.append, clock=lambda: 0.0)
    command = Command(command_id="c-1", robot_id="rover-a", type=type_, label=type_,
                      params=params, initiated_by="mallory")
    token = bind_actor("sue")
    try:
        audit.command_published("rover-a", command, error=error, site_id=SITE)
    finally:
        reset_actor(token)
    return rows


@pytest.mark.parametrize("type_, action", [("GREET", "greet"), ("SET_GREETER", "greeter.set")])
def test_each_gets_a_named_audit_row_beside_command_publish(type_, action):
    rows = _published(type_, BODIES[type_])
    assert [r.action for r in rows] == ["command.publish", action]
    named = rows[1]
    assert (named.actor, named.target, named.outcome, named.site_id, named.robot_id) == (
        "sue", "rover-a", "sent", SITE, "rover-a")
    assert named.detail == {"command_id": "c-1", "params": BODIES[type_]}


def test_a_greeting_that_was_not_sent_says_so_in_its_named_row():
    rows = _published("GREET", GREET, error="could not publish command to the broker (rc=4)")
    assert [(r.action, r.outcome) for r in rows] == [("command.publish", "not_sent"), ("greet", "not_sent")]


def test_other_commands_get_no_named_row():
    assert [r.action for r in _published("SET_DETECTION", {"enabled": True})] == ["command.publish"]
