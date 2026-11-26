"""Resume after E-Stop (schema 0.0.33): who may send RESUME, and the run
records that must keep an E-Stopped run open.

An E-Stop now pauses a mission run (ActiveRun.paused_for == "estop") instead
of ending it. Anyone who may drive may Resume that pause -- the person at the
console may not be the one who pressed E-Stop -- while a blocked leg's Retry
still sets a patrol going and needs dispatchPatrols. The backend decides per
request from the robot's latest /state, and refuses rather than widens when it
has none.

Spec: docs/superpowers/specs/2026-11-26-resume-after-estop-and-return-home-design.md
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.policy import RESUME_ESTOP_CAPABILITIES, command_capabilities  # noqa: E402
from runs.reaper import NAMED_GRACE_S, RunReaper  # noqa: E402
from tests import test_site_setup as site_setup  # noqa: E402
from tests.test_runs_dispatch import RUN_ID, Env  # noqa: E402
from tests.test_site_setup import _bearer, _command  # noqa: E402

world = site_setup.world
auth_on = site_setup.auth_on


def _paused(paused_for, run_id="run-1"):
    return {"run_id": run_id, "status": "paused", "started_at": "2026-11-26T02:00:00Z",
            "paused_for": paused_for, "redo_step_label": "Walk to C"}


def _state(active_run, robot="rover-a"):
    backend.registry.update_state(robot, "STAND", "ready", active_run=active_run, bridge_boot_id="boot-a")


def _resume(client, user, run_id="run-1"):
    return _command(client, "RESUME", {"run_id": run_id}, headers=_bearer(client, user))


def test_a_blocked_retry_still_needs_dispatch_patrols_and_an_estop_resume_drive():
    assert command_capabilities("RESUME") == ("dispatchPatrols",)
    assert RESUME_ESTOP_CAPABILITIES == ("drive",)


@pytest.mark.parametrize("user, allowed", [
    ("ann", True),      # operator
    ("sue", True),      # supervisor
    ("eve", True),      # engineer
    ("tom", False),     # technician: may E-Stop and get a robot up, does not drive
    ("vic", False),     # viewer
])
def test_anyone_who_may_drive_may_resume_an_estop_pause(auth_on, world, user, allowed):
    _state(_paused("estop"))
    response = _resume(TestClient(backend.app), user)
    assert response.status_code == (200 if allowed else 403), (user, response.text)


@pytest.mark.parametrize("user, allowed", [("ann", False), ("eve", False), ("sue", True)])
def test_a_blocked_retry_is_still_the_supervisors(auth_on, world, user, allowed):
    _state(_paused("blocked"))
    assert _resume(TestClient(backend.app), user).status_code == (200 if allowed else 403), user


def test_an_operator_may_not_resume_some_other_run(auth_on, world):
    # The pause the registry knows is run-1's; a RESUME naming another run is
    # judged as a Retry, so a stale tab cannot borrow the E-Stop rule.
    _state(_paused("estop"))
    client = TestClient(backend.app)
    assert _resume(client, "ann", run_id="run-2").status_code == 403
    assert _resume(client, "sue", run_id="run-2").status_code == 200


@pytest.mark.parametrize("active_run", [
    None,                                                   # no run, or no /state yet
    _paused("operator"),                                    # an operator's pause: Release & resume
    {**_paused("estop"), "status": "running"},              # not paused at all
])
def test_without_an_estop_pause_on_record_an_operator_is_refused(auth_on, world, active_run):
    _state(active_run)
    response = _resume(TestClient(backend.app), "ann")
    assert response.status_code == 403
    assert response.json()["detail"] == "your role does not allow RESUME"


def test_a_robot_that_never_reported_state_falls_back_to_dispatch_patrols(auth_on, world):
    client = TestClient(backend.app)
    assert _resume(client, "ann").status_code == 403
    assert _resume(client, "sue").status_code == 200


def test_sign_in_off_sends_it_whatever_the_pause(world):
    _state(_paused("blocked"))
    assert _command(TestClient(backend.app), "RESUME", {"run_id": "run-1"}).status_code == 200


# --- run records: an E-Stop pause keeps the run open ---------------------------


def test_an_estop_pause_and_the_estop_ack_leave_the_run_open(tmp_path):
    from shared.schemas.commands import Command

    env = Env(tmp_path)
    assert env.post().status_code == 201
    paused = {"run_id": RUN_ID, "seq": 1, "at": "2026-11-26T02:41:07Z", "kind": "paused",
              "block_id": "leg-3", "message": "Walk to C"}
    assert env.client.post(f"/runs/{RUN_ID}/events", json={"events": [paused]}).status_code == 200
    env.registry.update_state("rover-pi", "E_STOP", "passive", bridge_boot_id="boot-a",
                              active_run={**_paused("estop", RUN_ID)})

    estop = Command(command_id="c-estop", robot_id="rover-pi", type="ESTOP", label="E-Stop",
                    current_stage="execution_completed", stages=[],
                    initiated_by="ann")
    env.dispatcher.on_ack(estop)
    assert env.runs.get(RUN_ID).result == "running"

    # Held paused well past the reaper's grace: the bridge still names it.
    now = {"t": 1_000_000.0}
    failed = []
    reaper = RunReaper(env.registry, env.runs, lambda run_id, reason: failed.append(run_id), lambda: now["t"])
    reaper.sweep()
    now["t"] += NAMED_GRACE_S * 10
    reaper.sweep()
    assert failed == []
    assert env.runs.get(RUN_ID).result == "running"
