"""runs/reaper.py: fail a run the bridge no longer names (spec 6.7)."""
from datetime import datetime, timezone

import pytest

from robots.registry import RobotRegistry
from runs.reaper import RunReaper, NAMED_GRACE_S
from runs.store import RunStore
from shared.schemas.runs import MissionRun
from tasks.store import TaskStore
from tests.test_runs import _run

START = "2026-10-29T10:00:00Z"
T0 = datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc).timestamp()


class Env:
    def __init__(self, tmp_path):
        self.registry = RobotRegistry()
        self.registry.upsert_from_registration("rvr-01", "site-a", "quad", "b-1", "0.0.11")
        self.set_state(active_run=None)
        self.runs = RunStore(str(tmp_path / "runs"))
        self.tasks = TaskStore(str(tmp_path / "tasks"))
        self.now = T0
        self.changes = []
        self.failed = []

        def fail(run_id, reason):
            self.failed.append((run_id, reason))
            run = self.runs.get(run_id)
            run.result, run.end_reason = "failed", reason
            self.runs.put(run)
            occ = self.tasks.get_occurrence(run_id)
            if occ:
                occ.status = "failed"
                self.tasks.put_occurrence(occ)
        self.reaper = RunReaper(self.registry, self.runs, fail, lambda: self.now)

    def set_state(self, active_run, boot="boot-a"):
        self.registry.update_state("rvr-01", "STAND", "stand", active_run=active_run,
                                   bridge_boot_id=boot)

    def put(self, run_id="r-1", **extra):
        self.runs.put(MissionRun.model_validate(_run(
            run_id=run_id, started_at=START, bridge_boot_id="boot-a", source="editor", **extra)))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_an_unnamed_run_is_reaped_after_120s(env):
    env.put()
    env.reaper.sweep()                       # first sight at T0
    env.now = T0 + NAMED_GRACE_S - 1
    env.reaper.sweep()
    assert env.failed == []

    env.now = T0 + NAMED_GRACE_S + 1
    env.reaper.sweep()

    assert env.failed == [("r-1", "noAnswer")]


def test_a_run_younger_than_120s_is_never_reaped(env):
    env.put()
    env.reaper.sweep()
    env.now = T0 + 119
    env.reaper.sweep()
    assert env.failed == []


def test_a_named_run_is_never_reaped(env):
    env.put()
    env.reaper.sweep()
    for minute in range(1, 11):
        env.now = T0 + 60 * minute
        env.set_state(active_run={"run_id": "r-1"})
        env.reaper.sweep()
    assert env.failed == []


def test_naming_resets_the_clock(env):
    env.put()
    env.reaper.sweep()
    env.now = T0 + 200
    env.set_state(active_run={"run_id": "r-1"})
    env.reaper.sweep()
    env.set_state(active_run=None)
    env.now = T0 + 200 + NAMED_GRACE_S - 5
    env.reaper.sweep()
    assert env.failed == []
    env.now = T0 + 200 + NAMED_GRACE_S + 5
    env.reaper.sweep()
    assert env.failed == [("r-1", "noAnswer")]


def test_a_legacy_run_is_never_reaped(env):
    env.runs.put(MissionRun.model_validate(_run(run_id="legacy", started_at=START)))
    env.reaper.sweep()
    env.now = T0 + 10_000
    env.reaper.sweep()
    assert env.failed == []


@pytest.mark.parametrize("setup", [
    lambda e: e.registry.mark_offline("rvr-01"),
    lambda e: e.set_state(None, boot="boot-b"),   # a different boot is reconcile's job
    lambda e: e.set_state(None, boot=None),       # a bridge that reports no boot id
])
def test_not_reaped_when_the_robots_state_cannot_vouch(env, setup):
    env.put()
    setup(env)
    env.reaper.sweep()
    env.now = T0 + 10_000
    env.reaper.sweep()
    assert env.failed == []


def test_a_finished_run_is_not_reaped_and_forgotten(env):
    env.put(result="completed")
    env.reaper.sweep()
    env.now = T0 + 10_000
    env.reaper.sweep()
    assert env.failed == []


def test_a_failing_fail_callback_does_not_stop_the_sweep(env):
    env.put("r-1")
    env.put("r-2")
    calls = []

    def boom(run_id, reason):
        calls.append(run_id)
        raise RuntimeError("bucket down")
    env.reaper._fail = boom
    env.reaper.sweep()
    env.now = T0 + 1000
    env.reaper.sweep()
    assert sorted(calls) == ["r-1", "r-2"]


def test_grace_restarts_when_the_robot_comes_back_online(env):
    env.put()
    env.registry.mark_offline("rvr-01")
    env.reaper.sweep()
    env.now = T0 + 1000
    env.reaper.sweep()          # still offline: nothing, but the clock is reset
    env.registry.mark_seen("rvr-01")
    env.reaper.sweep()
    assert env.failed == []
    env.now = T0 + 1000 + NAMED_GRACE_S + 1
    env.reaper.sweep()
    assert env.failed == [("r-1", "noAnswer")]


def test_a_robot_that_went_silent_is_not_judged_until_it_returns(tmp_path):
    """A bridge that stops talking ages offline in the registry. Its run is
    neither reaped while it is silent (nothing can vouch either way) nor the
    moment it returns: the grace period restarts on its return."""

    wall = [datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc)]
    env = Env(tmp_path)
    env.registry = RobotRegistry(clock=lambda: wall[0], offline_after_s=15.0)
    env.registry.upsert_from_registration("rvr-01", "site-a", "quad", "b-1", "0.0.11")
    env.set_state(active_run=None)
    env.reaper._registry = env.registry
    env.put()

    def at(seconds):
        env.now = T0 + seconds
        wall[0] = datetime.fromtimestamp(T0 + seconds, tz=timezone.utc)

    at(0)
    env.reaper.sweep()
    at(NAMED_GRACE_S * 5)                    # silent all along: aged offline
    assert env.registry.get("rvr-01").online is False
    env.reaper.sweep()
    assert env.failed == []

    env.registry.mark_seen("rvr-01")         # its next heartbeat
    assert env.registry.get("rvr-01").online is True
    env.reaper.sweep()
    assert env.failed == []                  # grace restarted, not reaped on return
