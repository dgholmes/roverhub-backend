"""runs/orphans.py: settle what a previous backend process left in flight.

A backend that stops mid-run leaves occurrences "dispatched"/"running" and runs
"running" on disk. Nothing in a new process would ever close some of them -- an
occurrence whose run already ended, one whose run was never created, a run on a
robot that never comes back -- and an in-flight occurrence makes the scheduler
skip every later patrol of that robot as robot_busy. The settler ends them with
a reason, and must never end a run that its robot is still running.
"""
from datetime import datetime, timezone

import pytest

from control.commands import CommandStore
from robots.registry import RobotRegistry
from runs.dispatch import RunDispatcher
from runs.orphans import STATE_GRACE_S, OrphanSettler
from runs.store import RunStore
from shared.schemas.missions import MissionLibraryEntry
from shared.schemas.runs import MissionRun
from shared.schemas.tasks import MissionSchedule, MissionTask, TaskOccurrence
from tasks.store import TaskStore

STARTED = "2026-10-29T10:00:00Z"
T0 = datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc).timestamp()
RUN = "t-1-20261029T100000Z"


def _mission():
    return MissionLibraryEntry(
        mission_id="m-1", name="Patrol", type="patrol", kind="program", creator="x",
        updated_at="2026-10-23T00:00:00Z", map_id="map-1", site_id="site-a",
        program_state='{"blocks":{}}')


class Env:
    def __init__(self, tmp_path):
        self.registry = RobotRegistry(offline_after_s=None)
        self.runs = RunStore(str(tmp_path / "runs"))
        self.tasks = TaskStore(str(tmp_path / "tasks"))
        # Production's closer: settles the run, its occurrence and the bucket hook.
        self.dispatcher = RunDispatcher(
            self.registry, self.runs, self.tasks, CommandStore(), lambda robot_id, command: None,
            clock_iso=lambda: "2026-10-29T12:00:00Z")
        self.now = T0 + 3600  # the backend comes back an hour after the run started
        self.settler = OrphanSettler(
            self.registry, self.runs, self.tasks, self.dispatcher.start_failed,
            clock_s=lambda: self.now)

    def task(self, max_duration_min=None):
        self.tasks.put_task(MissionTask(
            task_id="t-1", mission_id="m-1", site_id="site-a", robot_id="rvr-01",
            schedule=MissionSchedule(schedule_id="s-1", recurrence="daily",
                                     start_at="2026-10-20T19:00", timezone="Asia/Seoul"),
            max_duration_min=max_duration_min, created_by="x",
            created_at="2026-10-23T00:00:00Z", updated_at="2026-10-23T00:00:00Z"))

    def occurrence(self, status="running", run_id=RUN, occurrence_id=RUN):
        occ = TaskOccurrence(
            occurrence_id=occurrence_id, task_id="t-1", mission_id="m-1", site_id="site-a",
            robot_id="rvr-01", due_at=STARTED, status=status, run_id=run_id)
        self.tasks.put_occurrence(occ)
        return occ

    def run(self, run_id=RUN, **extra):
        fields = dict(run_id=run_id, mission_id="m-1", mission=_mission(), robot_id="rvr-01",
                      site_id="site-a", started_at=STARTED, started_by="schedule",
                      source="schedule", bridge_boot_id="boot-a")
        fields.update(extra)
        run = MissionRun(**fields)
        self.runs.put(run)
        return run

    def report(self, active_run=None, boot="boot-a"):
        """One state message from the robot, as BackendMqttClient applies it."""
        if self.registry.get("rvr-01") is None:
            self.registry.upsert_from_registration("rvr-01", "site-a", "quad", "b-1", "0.0.11")
        self.registry.update_state("rvr-01", "STAND", "stand", bridge_boot_id=boot,
                                   active_run={"run_id": active_run} if active_run else None)
        self.settler.note_state("rvr-01")

    def occ_status(self, occurrence_id=RUN):
        return self.tasks.get_occurrence(occurrence_id).status


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# -- settled at once: nothing about them depends on what a robot says ---------

def test_an_occurrence_left_running_whose_run_already_ended_takes_the_runs_outcome(env):
    # The wedge itself, as the old scheduler left it on disk.
    env.task()
    env.run(result="failed", ended_at="2026-10-29T10:00:01Z", end_reason="REJECTED_CONTROL_HELD")
    env.occurrence("running")

    env.settler.start()

    occ = env.tasks.get_occurrence(RUN)
    assert (occ.status, occ.message) == ("failed", "REJECTED_CONTROL_HELD")


def test_a_completed_run_settles_its_stuck_occurrence_as_succeeded(env):
    env.task()
    env.run(result="completed", ended_at="2026-10-29T10:05:00Z")
    env.occurrence("dispatched")

    env.settler.start()

    assert env.occ_status() == "succeeded"


def test_an_in_flight_occurrence_with_no_run_is_failed_with_the_reason(env):
    # The process stopped between writing "dispatched" and creating the run, so
    # nothing was ever sent.
    env.task()
    env.occurrence("dispatched")

    env.settler.start()

    occ = env.tasks.get_occurrence(RUN)
    assert occ.status == "failed"
    assert "never sent" in occ.message


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "skipped", "scheduled", "due"])
def test_an_occurrence_that_is_not_in_flight_is_never_touched(env, status):
    env.task()
    env.run(result="running")
    env.occurrence(status)

    env.settler.start()
    env.now += 10 * STATE_GRACE_S
    env.settler.sweep()

    assert env.occ_status() == status


# -- a run still "running": judged only once its robot can answer -------------

def test_an_orphan_its_robot_no_longer_names_is_failed_after_the_grace(env):
    env.task()
    env.run()
    env.occurrence("running")
    env.settler.start()

    env.report(active_run=None)            # first state since start-up
    env.settler.sweep()
    env.now += STATE_GRACE_S - 1
    env.settler.sweep()
    assert env.runs.get(RUN).result == "running"

    env.now += 2
    env.settler.sweep()
    run = env.runs.get(RUN)
    assert (run.result, run.end_reason) == ("failed", "noAnswer")
    assert env.occ_status() == "failed"


def test_an_orphan_on_a_bridge_with_a_different_boot_id_is_bridge_restarted(env):
    env.task()
    env.run(bridge_boot_id="boot-a")
    env.occurrence("running")
    env.settler.start()

    env.report(active_run=None, boot="boot-b")
    env.now += STATE_GRACE_S + 1
    env.settler.sweep()

    run = env.runs.get(RUN)
    assert (run.result, run.end_reason) == ("failed", "bridgeRestarted")
    assert env.occ_status() == "failed"


def test_an_orphan_on_a_robot_that_names_another_run_is_failed(env):
    env.task()
    env.run()
    env.occurrence("running")
    env.settler.start()

    env.report(active_run="some-other-run")
    env.now += STATE_GRACE_S + 1
    env.settler.sweep()

    assert env.runs.get(RUN).end_reason == "noAnswer"


def test_an_orphan_older_than_its_cap_on_a_robot_that_never_reports_is_time_limit(env):
    env.task(max_duration_min=30)          # the run started an hour ago
    env.run()
    env.occurrence("running")
    env.settler.start()

    env.settler.sweep()                    # the robot may still report: wait
    assert env.runs.get(RUN).result == "running"

    env.now += STATE_GRACE_S + 1
    env.settler.sweep()
    run = env.runs.get(RUN)
    assert (run.result, run.end_reason) == ("failed", "timeLimit")
    assert env.occ_status() == "failed"


def test_an_orphan_with_no_run_record_for_its_robot_but_a_scheduler_source_is_judged(env):
    # Scheduled before the robot's boot id was known: no boot id, but a source,
    # so a bridge could have closed it -- the dispatcher counts it as running.
    env.task()
    env.run(bridge_boot_id=None)
    env.occurrence("running")
    env.settler.start()

    env.report(active_run=None)
    env.now += STATE_GRACE_S + 1
    env.settler.sweep()

    assert env.runs.get(RUN).end_reason == "noAnswer"


# -- and the other side: never end a run a robot is running --------------------

def test_a_live_run_its_robot_reports_is_left_alone(env):
    env.task(max_duration_min=30)          # older than its cap: paused time is not running time
    env.run()
    env.occurrence("running")
    env.settler.start()

    env.report(active_run=RUN)
    for _ in range(5):
        env.now += STATE_GRACE_S
        env.report(active_run=RUN)
        env.settler.sweep()

    assert env.runs.get(RUN).result == "running"
    assert env.occ_status() == "running"


def test_a_robot_that_has_not_reported_yet_is_waited_for(env):
    # Registered (the retained registration arrived) but no state yet, and no
    # cap to bound the run: nothing says the run is over, so it stays.
    env.task()
    env.run()
    env.occurrence("running")
    env.registry.upsert_from_registration("rvr-01", "site-a", "quad", "b-1", "0.0.11")
    env.settler.start()

    env.now += 10 * STATE_GRACE_S
    env.settler.sweep()

    assert env.runs.get(RUN).result == "running"
    assert env.occ_status() == "running"


def test_a_run_younger_than_its_cap_on_a_silent_robot_is_left_alone(env):
    env.task(max_duration_min=120)
    env.run()
    env.occurrence("running")
    env.settler.start()

    env.now += STATE_GRACE_S + 1
    env.settler.sweep()

    assert env.runs.get(RUN).result == "running"


def test_a_run_that_stops_being_named_gets_the_grace_again(env):
    env.task()
    env.run()
    env.occurrence("running")
    env.settler.start()
    env.report(active_run=RUN)
    env.now += 10 * STATE_GRACE_S
    env.settler.sweep()

    env.report(active_run=None)            # its run_ended may be a moment behind
    env.now += 1
    env.settler.sweep()
    assert env.runs.get(RUN).result == "running"


def test_a_run_that_ends_normally_is_left_with_its_own_outcome(env):
    env.task()
    env.run()
    env.occurrence("running")
    env.settler.start()

    ended = env.runs.get(RUN).model_copy(update={
        "result": "completed", "ended_at": "2026-10-29T11:00:05Z"})
    env.runs.put(ended)
    env.report(active_run=None)
    env.now += STATE_GRACE_S + 1
    env.settler.sweep()

    run = env.runs.get(RUN)
    assert (run.result, run.end_reason) == ("completed", None)
    assert env.occ_status() == "succeeded"


def test_a_browser_run_with_no_source_and_no_boot_id_is_left_alone(env):
    # The same policy as the reaper and the dispatcher: a bridge never knew it.
    env.run(run_id="legacy-run", source=None, bridge_boot_id=None, started_by="operator-dev")
    env.settler.start()
    env.report(active_run=None)
    env.now += STATE_GRACE_S + 1
    env.settler.sweep()

    assert env.runs.get("legacy-run").result == "running"


def test_runs_started_by_this_process_are_not_orphans(env):
    env.settler.start()
    env.task()
    env.run()                              # created after start-up: the reaper's, not ours
    env.report(active_run=None)
    env.now += STATE_GRACE_S + 1
    env.settler.sweep()

    assert env.runs.get(RUN).result == "running"
