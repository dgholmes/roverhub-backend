"""SchedulerLoop.run_once(): the I/O around materialise() and decide().

The logic lives in those two and is tested there. What is tested here is what
only the tick does: write outcomes back, never overwrite an occurrence's
outcome, see a robot as busy within one tick, cancel for a paused task, survive
a bad record, and not repeat one warning every 30 seconds.
"""
import logging
from datetime import datetime, timezone

import pytest

from missions.router import MissionStore
from robots.registry import RobotRegistry
from runs.store import RunStore
from shared.schemas.missions import MissionEstimates, MissionLibraryEntry
from shared.schemas.tasks import (
    DispatchRule, MissionSchedule, MissionTask, TaskOccurrence,
)
from tasks.scheduler import EXECUTOR_KINDS, SchedulerLoop, _RepeatFilter
from tasks.store import TaskStore

DUE = "2026-10-24T09:00:00Z"
AT_DUE = datetime(2026, 10, 24, 9, 0, 30, tzinfo=timezone.utc)
BEFORE = datetime(2026, 10, 24, 8, 59, 30, tzinfo=timezone.utc)


class _World:
    def __init__(self, tmp_path, executor_kinds=EXECUTOR_KINDS, publish=None):
        self.store = TaskStore(str(tmp_path / "tasks"))
        self.registry = RobotRegistry()
        self.registry.upsert_from_registration("robot-1", "site-a", "quad")
        self.missions = MissionStore(str(tmp_path / "missions"))
        self.runs = RunStore(str(tmp_path / "runs"))
        self.published = []
        self.loop = SchedulerLoop(
            self.store, self.registry, self.missions,
            publish or (lambda occ, run: self.published.append((occ, run))),
            run_store=self.runs, executor_kinds=executor_kinds,
        )
        self.missions.put(MissionLibraryEntry(
            mission_id="m-1", name="Patrol", type="patrol", kind="program",
            creator="x", updated_at="2026-10-23T00:00:00Z", map_id="map-1",
            site_id="site-a", program_state='{"blocks":{}}',
            estimates=MissionEstimates(duration_bounded=True),
        ))

    def task(self, task_id="t-1", status="enabled", robot_id="robot-1", **rule):
        task = MissionTask(
            task_id=task_id, mission_id="m-1", site_id="site-a", robot_id=robot_id,
            schedule=MissionSchedule(
                schedule_id="s-1", recurrence="daily", start_at="2026-10-20T18:00",
                timezone="Asia/Seoul",
            ),
            dispatch_rule=DispatchRule(**rule), status=status, created_by="operator-dev",
            created_at="2026-10-23T00:00:00Z", updated_at="2026-10-23T00:00:00Z",
        )
        self.store.put_task(task)
        return task

    def due(self, task_id="t-1", due_at=DUE, robot_id="robot-1", **extra):
        occ = TaskOccurrence(
            occurrence_id=f"{task_id}-{due_at.replace('-', '').replace(':', '')}",
            task_id=task_id, mission_id="m-1", site_id="site-a", robot_id=robot_id,
            due_at=due_at, **extra,
        )
        self.store.put_occurrence(occ)
        return occ


def test_a_due_occurrence_is_skipped_with_the_reason_written_back(tmp_path):
    world = _World(tmp_path)  # no executors: the plan-1 state
    world.task()
    occ = world.due()

    world.loop.run_once(AT_DUE)

    stored = world.store.get_occurrence(occ.occurrence_id)
    assert (stored.status, stored.skip_reason) == ("skipped", "no_executor")
    assert "program" in stored.message
    assert world.published == []


def test_a_battery_rule_skips_with_an_unavailable_message_because_the_cloud_has_no_reading(tmp_path):
    # The tick passes what the registry has, which is nothing. The operator
    # must be told the reading is unavailable, not that the battery is low.
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    world.task(min_battery_pct=20)
    occ = world.due()

    world.loop.run_once(AT_DUE)

    stored = world.store.get_occurrence(occ.occurrence_id)
    assert (stored.status, stored.skip_reason) == ("skipped", "dispatch_rule")
    assert "unavailable" in stored.message
    assert world.published == []


def test_an_occurrence_not_yet_due_is_left_alone(tmp_path):
    world = _World(tmp_path)
    world.task()
    occ = world.due()

    world.loop.run_once(BEFORE)

    assert world.store.get_occurrence(occ.occurrence_id).status == "scheduled"


def test_materialise_then_come_due_over_two_ticks(tmp_path):
    world = _World(tmp_path)
    world.task()

    world.loop.run_once(BEFORE)
    ahead = world.store.get_occurrence("t-1-20261024T090000Z")
    assert ahead is not None and ahead.status == "scheduled"

    world.loop.run_once(AT_DUE)
    assert world.store.get_occurrence("t-1-20261024T090000Z").status == "skipped"


def test_rematerialising_never_overwrites_an_occurrence_that_left_scheduled(tmp_path):
    world = _World(tmp_path)
    world.task()
    world.loop.run_once(BEFORE)
    world.loop.run_once(AT_DUE)  # skipped now

    # A tick whose window still contains that slot would regenerate it as
    # "scheduled"; the stored outcome must survive.
    world.loop.run_once(BEFORE)

    stored = world.store.get_occurrence("t-1-20261024T090000Z")
    assert (stored.status, stored.skip_reason) == ("skipped", "no_executor")


def test_a_healthy_program_is_dispatched_with_a_run_started_by_schedule(tmp_path):
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    world.task()
    occ = world.due()

    world.loop.run_once(AT_DUE)

    stored = world.store.get_occurrence(occ.occurrence_id)
    assert stored.status == "running" and stored.run_id == occ.occurrence_id
    run = world.runs.get(stored.run_id)
    assert (run.started_by, run.robot_id, run.mission_id, run.result) == (
        "schedule", "robot-1", "m-1", "running",
    )
    assert run.mission.kind == "program"
    assert [(o.status, r.run_id) for o, r in world.published] == [("dispatched", run.run_id)]


def test_two_occurrences_due_together_for_one_robot_do_not_both_dispatch(tmp_path):
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    world.task("t-1")
    world.task("t-2")
    first = world.due("t-1")
    second = world.due("t-2")

    world.loop.run_once(AT_DUE)

    assert world.store.get_occurrence(first.occurrence_id).status == "running"
    stored = world.store.get_occurrence(second.occurrence_id)
    assert (stored.status, stored.skip_reason) == ("skipped", "robot_busy")


def test_a_robot_with_a_mission_in_flight_from_an_earlier_tick_is_busy(tmp_path):
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    world.task("t-1")
    world.due("t-1", due_at="2026-10-23T09:00:00Z", status="running", run_id="r-0")
    waiting = world.due("t-1")

    world.loop.run_once(AT_DUE)

    stored = world.store.get_occurrence(waiting.occurrence_id)
    assert (stored.status, stored.skip_reason) == ("skipped", "robot_busy")


def test_a_failed_publish_marks_the_occurrence_and_run_failed_and_frees_the_robot(tmp_path):
    attempts = []

    def publish(occ, run):
        attempts.append(occ.occurrence_id)
        if len(attempts) == 1:
            raise RuntimeError("broker down")

    world = _World(tmp_path, executor_kinds=frozenset({"program"}), publish=publish)
    world.task("t-1")
    world.task("t-2")
    first = world.due("t-1")
    second = world.due("t-2")

    world.loop.run_once(AT_DUE)

    failed = world.store.get_occurrence(first.occurrence_id)
    assert failed.status == "failed" and "broker down" in failed.message
    assert world.runs.get(failed.run_id).result == "failed"
    # The failed dispatch did not hold the robot: the next one went.
    assert world.store.get_occurrence(second.occurrence_id).status == "running"


@pytest.mark.parametrize("status", ["paused", "archived"])
def test_an_occurrence_of_a_task_no_longer_enabled_is_cancelled_not_dispatched(tmp_path, status):
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    world.task(status=status)
    occ = world.due()

    world.loop.run_once(AT_DUE)

    stored = world.store.get_occurrence(occ.occurrence_id)
    assert stored.status == "cancelled" and status in stored.message
    assert world.published == []


def test_an_occurrence_of_a_deleted_task_is_cancelled(tmp_path):
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    occ = world.due()  # no task written

    world.loop.run_once(AT_DUE)

    stored = world.store.get_occurrence(occ.occurrence_id)
    assert stored.status == "cancelled" and "deleted" in stored.message


def test_a_mission_the_library_rejects_does_not_stop_the_other_occurrences(tmp_path):
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    world.task("t-1")
    world.task("t-2", robot_id="robot-1")
    bad = world.due("t-1").model_copy(update={"mission_id": "../escape"})
    world.store.put_occurrence(bad)
    good = world.due("t-2")

    world.loop.run_once(AT_DUE)

    stored = world.store.get_occurrence(bad.occurrence_id)
    assert (stored.status, stored.skip_reason) == ("skipped", "mission_missing")
    assert world.store.get_occurrence(good.occurrence_id).status == "running"


def test_a_mission_file_that_is_present_but_unreadable_is_reported_as_such(tmp_path):
    # MissionStore.get returns None for a schema-invalid file (one that predates
    # `kind`, say), the same as for an absent one. The operator must not be told
    # the mission was deleted.
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    world.task()
    (tmp_path / "missions" / "m-1.json").write_text('{"mission_id": "m-1"}', encoding="utf-8")
    occ = world.due()

    world.loop.run_once(AT_DUE)

    stored = world.store.get_occurrence(occ.occurrence_id)
    assert (stored.status, stored.skip_reason) == ("skipped", "mission_missing")
    assert "missing or unreadable" in stored.message and "deleted" not in stored.message


def test_an_occurrence_that_blows_up_does_not_stop_the_others(tmp_path, monkeypatch):
    # The per-occurrence try/except is what makes "one bad record must not stop
    # every patrol on the site" true; nothing in decide() can raise to exercise
    # it, so a store write is made to.
    world = _World(tmp_path, executor_kinds=frozenset({"program"}))
    world.registry.upsert_from_registration("robot-2", "site-a", "quad")
    world.task("t-1")
    world.task("t-2", robot_id="robot-2")
    bad = world.due("t-1")
    good = world.due("t-2", robot_id="robot-2")
    real_put = world.runs.put

    def put(run):
        if run.run_id == bad.occurrence_id:
            raise OSError("disk full")
        real_put(run)

    monkeypatch.setattr(world.runs, "put", put)

    world.loop.run_once(AT_DUE)

    assert world.store.get_occurrence(good.occurrence_id).status == "running"
    # Pins what actually happens to the one that failed (see _dispatch): it is
    # left "dispatched" with no run, and nothing here reaps it.
    stuck = world.store.get_occurrence(bad.occurrence_id)
    assert stuck.status == "dispatched" and world.runs.get(bad.occurrence_id) is None


# -- warning repetition -----------------------------------------------------

def test_a_repeat_warning_is_let_through_once_per_quiet_period():
    now = [0.0]
    flt = _RepeatFilter(quiet_s=100, clock=lambda: now[0])

    def warn(msg, *args):
        return logging.LogRecord("roverhub.tasks", logging.WARNING, "f", 1, msg, args, None)

    bad = "task %s: schedule cannot be evaluated: %s"
    # The exception is a new object every tick; only its text is the same.
    assert flt.filter(warn(bad, "t-1", ValueError("no such zone")))
    now[0] = 30
    assert not flt.filter(warn(bad, "t-1", ValueError("no such zone")))
    # A different task is a different problem, and is not hidden by the first.
    assert flt.filter(warn(bad, "t-2", ValueError("no such zone")))
    now[0] = 101
    assert flt.filter(warn(bad, "t-1", ValueError("no such zone")))


def test_distinct_exceptions_from_the_same_logging_call_are_not_swallowed():
    # `logger.exception("scheduler tick failed")` has a constant message, so a
    # filter keyed on text alone would hide every failure after the first --
    # and its traceback. Disk full, then a corrupt store file: both must show.
    now = [0.0]
    flt = _RepeatFilter(quiet_s=100, clock=lambda: now[0])

    def error(exc):
        try:
            raise exc
        except Exception:
            import sys
            return logging.LogRecord(
                "roverhub.tasks", logging.ERROR, "f", 1, "scheduler tick failed", (), sys.exc_info(),
            )

    assert flt.filter(error(OSError("disk full")))
    now[0] = 30
    assert flt.filter(error(ValueError("corrupt store file")))
    assert flt.filter(error(OSError("a different OSError")))
    # The same failure again, with a fresh exception object, is a repeat.
    assert not flt.filter(error(OSError("disk full")))


@pytest.mark.asyncio
async def test_a_loop_logs_two_different_tick_failures_but_not_a_repeat(tmp_path, caplog):
    import asyncio
    world = _World(tmp_path)
    world.loop._interval_s = 0.005
    failures = [OSError("disk full"), ValueError("corrupt store file"), OSError("disk full")]
    calls = []

    def run_once(now):
        calls.append(now)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]

    world.loop.run_once = run_once
    caplog.set_level(logging.WARNING, logger="roverhub.tasks")

    task = asyncio.create_task(world.loop.run())
    for _ in range(200):
        if len(calls) > len(failures):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    logged = [
        (r.exc_info[0].__name__, str(r.exc_info[1]))
        for r in caplog.records if r.getMessage() == "scheduler tick failed"
    ]
    assert logged == [("OSError", "disk full"), ("ValueError", "corrupt store file")]


def test_info_records_are_never_suppressed():
    flt = _RepeatFilter(quiet_s=100, clock=lambda: 0.0)
    info = logging.LogRecord("roverhub.tasks", logging.INFO, "f", 1, "same", (), None)
    assert flt.filter(info) and flt.filter(info)


@pytest.mark.asyncio
async def test_run_installs_and_removes_the_filter(tmp_path):
    import asyncio
    world = _World(tmp_path)
    logger = logging.getLogger("roverhub.tasks")
    before = list(logger.filters)

    task = asyncio.create_task(world.loop.run())
    await asyncio.sleep(0.05)
    assert any(isinstance(f, _RepeatFilter) for f in logger.filters)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert logger.filters == before
