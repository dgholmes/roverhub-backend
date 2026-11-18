"""A stuck walk (schema 0.0.27) through POST /robots/{id}/commands: NAV_RESOLVE
needs what NAV_GOTO needs, SET_NAV_STUCK_POLICY what a robot setting needs;
both have their params checked and an older bridge is refused with the reason."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.policy import command_capabilities  # noqa: E402
from control.router import NAV_STUCK_SCHEMA_VERSION  # noqa: E402
from shared.schemas.commands import WIRED_COMMAND_TYPES, command_ttl_s  # noqa: E402
from tests import test_site_setup as site_setup  # noqa: E402
from tests.test_site_setup import DEVICE_A, SITE, _bearer, _command  # noqa: E402

world = site_setup.world
auth_on = site_setup.auth_on

RESOLVE = {"goal_id": "goal-1", "choice": "route", "option_id": "A"}
POLICY = {"policy": "auto"}


def test_both_are_wired_with_their_capabilities():
    assert {"NAV_RESOLVE", "SET_NAV_STUCK_POLICY"} <= WIRED_COMMAND_TYPES
    assert command_capabilities("NAV_RESOLVE") == command_capabilities("NAV_GOTO") == ("drive",)
    assert command_capabilities("SET_NAV_STUCK_POLICY") == command_capabilities("SET_LIDAR_MOUNT") == ("configure",)
    assert command_ttl_s("NAV_RESOLVE") == command_ttl_s("NAV_GOTO")
    assert NAV_STUCK_SCHEMA_VERSION == "0.0.27"


@pytest.mark.parametrize("command_type, params", [("NAV_RESOLVE", RESOLVE), ("SET_NAV_STUCK_POLICY", POLICY)])
def test_sign_in_off_sends_it_with_its_params(world, command_type, params):
    response = _command(TestClient(backend.app), command_type, params)
    assert response.status_code == 200, response.text
    sent, = world["sent"]
    assert sent.type == command_type and sent.params == params


@pytest.mark.parametrize("user, resolve, policy", [
    ("eve", True, True),                    # engineer
    ("ann", True, False),                   # operator: drives, does not configure robots
    ("vic", False, False),                  # viewer
])
def test_who_may_send_them(auth_on, world, user, resolve, policy):
    client = TestClient(backend.app)
    got = _command(client, "NAV_RESOLVE", RESOLVE, headers=_bearer(client, user)).status_code
    assert got == (200 if resolve else 403), user
    got = _command(client, "SET_NAV_STUCK_POLICY", POLICY, headers=_bearer(client, user)).status_code
    assert got == (200 if policy else 403), user


def test_a_device_key_cannot_send_them(auth_on, world):
    client = TestClient(backend.app)
    assert _command(client, "NAV_RESOLVE", RESOLVE, headers=DEVICE_A).status_code == 403
    assert _command(client, "SET_NAV_STUCK_POLICY", POLICY, headers=DEVICE_A).status_code == 403


@pytest.mark.parametrize("command_type, params, detail", [
    ("NAV_RESOLVE", {"goal_id": "g", "choice": "route"}, "an answer to a stuck walk (params)"),
    ("NAV_RESOLVE", {"goal_id": "g", "choice": "skip"}, "an answer to a stuck walk (choice)"),
    ("NAV_RESOLVE", {"choice": "cancel"}, "an answer to a stuck walk (goal_id)"),
    ("SET_NAV_STUCK_POLICY", {"policy": "maybe"}, "a stuck policy (ask, auto or stop) (policy)"),
])
def test_unusable_params_are_a_422(world, command_type, params, detail):
    response = _command(TestClient(backend.app), command_type, params)
    assert response.status_code == 422
    assert response.json()["detail"] == f"{command_type} params are not {detail}"
    assert world["sent"] == []


def test_a_bridge_older_than_0_0_27_is_refused_with_the_reason(world):
    backend.registry.upsert_from_registration("rover-026", SITE, "quad", schema_version="0.0.26")
    for command_type, params in (("NAV_RESOLVE", RESOLVE), ("SET_NAV_STUCK_POLICY", POLICY)):
        response = _command(TestClient(backend.app), command_type, params, robot="rover-026")
        assert response.status_code == 409
        assert response.json()["detail"] == "update the edge device: it runs schema 0.0.26, stuck walks need 0.0.27"
    assert world["sent"] == []
