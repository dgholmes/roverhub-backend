"""decide(): the dispatch gate (tasks/scheduler.py).

The skip reasons are read by an operator asking "why didn't my patrol run?", so
the tests pin the reason *and* that the message names the rule that failed. The
branch order (spec 6.1: window, executor, robot, rules) is pinned by tests that
make two branches fail at once and assert which one wins -- a misordered gate
passes every test where only one branch fails.
"""
from datetime import datetime, timedelta, timezone

from robots.registry import RobotRecord
from shared.schemas.missions import MissionEstimates, MissionLibraryEntry
from shared.schemas.tasks import (
    DispatchRule, MissionSchedule, MissionTask, TaskOccurrence,
)
from tasks.scheduler import EXECUTOR_KINDS, decide

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
DUE = "2026-10-24T09:00:00Z"
PROGRAMS = frozenset({"program"})


def _mission(kind="program", duration_bounded=True):
    extra = {"program_state": '{"blocks":{}}'} if kind == "program" else {}
    return MissionLibraryEntry(
        mission_id="m-1", name="Patrol", type="patrol", kind=kind, creator="x",
        updated_at="2026-10-23T00:00:00Z", map_id="map-1", site_id="site-a",
        estimates=MissionEstimates(duration_bounded=duration_bounded), **extra,
    )


def _task(window_minutes=None, max_duration_min=None, **rule):
    schedule = MissionSchedule(
        schedule_id="s-1", recurrence="daily", start_at="2026-10-20T18:00",
        timezone="Asia/Seoul", window_minutes=window_minutes,
    )
    return MissionTask(
        task_id="t-1", mission_id="m-1", site_id="site-a", robot_id="robot-1",
        schedule=schedule, dispatch_rule=DispatchRule(**rule),
        max_duration_min=max_duration_min, created_by="operator-dev",
        created_at="2026-10-23T00:00:00Z", updated_at="2026-10-23T00:00:00Z",
    )


def _occ(robot_id="robot-1", due_at=DUE):
    return TaskOccurrence(
        occurrence_id="t-1-20261024T090000Z", task_id="t-1", mission_id="m-1",
        site_id="site-a", robot_id=robot_id, due_at=due_at,
    )


def _robot(online=True):
    return RobotRecord(
        robot_id="robot-1", site_id="site-a", robot_type="quad", name="Rover", online=online,
    )


def _decide(*, occ=None, task=None, mission=None, robot="default", now=NOW,
            executor_kinds=PROGRAMS, **kwargs):
    # A healthy program on a healthy robot unless a test says otherwise, so each
    # test names only what it is breaking.
    return decide(
        occ or _occ(), task or _task(), mission or _mission(),
        _robot() if robot == "default" else robot, now, executor_kinds, **kwargs,
    )


def _skip(verdict, reason):
    assert (verdict.decision, verdict.skip_reason) == ("skip", reason), verdict


# -- executor ---------------------------------------------------------------

def test_every_kind_is_skipped_when_no_executor_is_configured():
    # The module default is empty in this plan: nothing on the edge can receive
    # either kind, so the scheduler must not claim a dispatch no component can
    # accept. Called with the default, not with an explicit empty set.
    assert EXECUTOR_KINDS == frozenset()
    for kind in ("route", "program"):
        verdict = decide(_occ(), _task(), _mission(kind), _robot(), NOW)
        _skip(verdict, "no_executor")
        assert kind in verdict.message


def test_route_is_skipped_as_no_executor_when_only_programs_have_one():
    verdict = _decide(mission=_mission("route"))
    _skip(verdict, "no_executor")
    assert "route" in verdict.message


def test_a_mission_missing_from_the_library_is_skipped_with_its_id_named():
    # Called directly: `_decide` substitutes a healthy mission for None.
    verdict = decide(_occ(), _task(), None, _robot(), NOW, PROGRAMS)
    _skip(verdict, "no_executor")
    assert "m-1" in verdict.message and "library" in verdict.message


# -- window -----------------------------------------------------------------

def test_missed_window_is_skipped_before_the_executor_check():
    # No executor at all AND the window is gone: the window is the truer story.
    late = NOW + timedelta(minutes=31)
    verdict = _decide(task=_task(window_minutes=30), now=late, executor_kinds=frozenset())
    _skip(verdict, "window_missed")
    assert "30" in verdict.message


def test_missed_window_is_skipped_even_for_a_program_that_could_run():
    late = NOW + timedelta(minutes=31)
    _skip(_decide(task=_task(window_minutes=30), now=late), "window_missed")


def test_missed_window_wins_over_an_unavailable_robot():
    late = NOW + timedelta(minutes=31)
    _skip(_decide(task=_task(window_minutes=30), now=late, robot=None), "window_missed")


def test_window_edge_is_inclusive():
    # "Not dispatched within N minutes": exactly N minutes late is still within.
    task = _task(window_minutes=30)
    on_the_edge = NOW + timedelta(minutes=30)
    assert _decide(task=task, now=on_the_edge).decision == "dispatch"
    _skip(_decide(task=task, now=on_the_edge + timedelta(seconds=1)), "window_missed")


def test_no_window_means_a_late_patrol_still_goes():
    very_late = NOW + timedelta(days=3)
    assert _decide(task=_task(window_minutes=None), now=very_late).decision == "dispatch"


def test_task_with_no_schedule_has_no_window():
    task = _task().model_copy(update={"schedule": None})
    assert _decide(task=task, now=NOW + timedelta(days=3)).decision == "dispatch"


def test_unparseable_due_at_with_a_window_cannot_be_checked_and_is_skipped():
    # Fail closed: a window that cannot be measured is not a window that passed.
    verdict = _decide(occ=_occ(due_at="not-a-time"), task=_task(window_minutes=30))
    _skip(verdict, "window_missed")
    assert "not-a-time" in verdict.message


# -- executor before robot -------------------------------------------------

def test_executor_is_checked_before_the_robot():
    verdict = _decide(robot=None, executor_kinds=frozenset())
    _skip(verdict, "no_executor")


# -- robot ------------------------------------------------------------------

def test_unset_robot_is_skipped():
    verdict = _decide(occ=_occ(robot_id=None), robot=None)
    _skip(verdict, "robot_unavailable")
    assert "no robot" in verdict.message


def test_robot_unknown_to_the_registry_is_skipped():
    verdict = _decide(robot=None)
    _skip(verdict, "robot_unavailable")
    assert "robot-1" in verdict.message


def test_robot_already_running_an_occurrence_is_skipped():
    verdict = _decide(robot_busy=True)
    _skip(verdict, "robot_busy")
    assert "robot-1" in verdict.message


def test_robot_is_checked_before_the_rules():
    # Unknown robot AND a battery rule that would fail: the robot is the story.
    verdict = _decide(task=_task(min_battery_pct=50), robot=None, battery_pct=5)
    _skip(verdict, "robot_unavailable")
    # Busy AND a failing rule.
    verdict = _decide(task=_task(min_battery_pct=50), robot_busy=True, battery_pct=5)
    _skip(verdict, "robot_busy")


# -- rules: battery ---------------------------------------------------------

def test_battery_below_min_is_skipped():
    verdict = _decide(task=_task(min_battery_pct=30), battery_pct=12)
    _skip(verdict, "dispatch_rule")
    assert "min_battery_pct" in verdict.message
    assert "12" in verdict.message and "30" in verdict.message


def test_battery_at_or_above_min_passes():
    task = _task(min_battery_pct=30)
    assert _decide(task=task, battery_pct=30).decision == "dispatch"
    assert _decide(task=task, battery_pct=90).decision == "dispatch"


def test_unknown_battery_with_a_threshold_is_skipped_not_permitted():
    # None is never "fine" (audit F1; `None < 30` being falsy is the bug this
    # guards against), and the message must say the reading is unavailable --
    # NOT that the battery is low. The robot may be at 90%.
    verdict = _decide(task=_task(min_battery_pct=15), battery_pct=None)
    _skip(verdict, "dispatch_rule")
    assert "min_battery_pct" in verdict.message
    assert "unavailable" in verdict.message
    assert "below" not in verdict.message and "low" not in verdict.message.split()


def test_unknown_battery_with_no_threshold_is_not_a_reason_to_skip():
    assert _decide(task=_task(min_battery_pct=None), battery_pct=None).decision == "dispatch"


def test_a_nan_battery_reading_is_unknown_not_permitted():
    # `nan < 30` is False, so a NaN would sail through a naive comparison.
    verdict = _decide(task=_task(min_battery_pct=15), battery_pct=float("nan"))
    _skip(verdict, "dispatch_rule")
    assert "unavailable" in verdict.message


def test_a_threshold_of_zero_is_still_a_threshold():
    # The edge treats 0 as "no gate" (SAFETY_MIN_BATTERY_PCT=0); here a set
    # threshold with no reading skips, whatever its value. Pinned so the
    # difference from the edge is a decision, not an accident.
    _skip(_decide(task=_task(min_battery_pct=0), battery_pct=None), "dispatch_rule")


# -- rules: network ---------------------------------------------------------

def test_offline_robot_is_skipped_when_the_network_is_required():
    verdict = _decide(task=_task(require_network=True), robot=_robot(online=False))
    _skip(verdict, "dispatch_rule")
    assert "require_network" in verdict.message and "offline" in verdict.message


def test_offline_robot_dispatches_when_the_network_is_not_required():
    # What makes `require_network` a rule rather than a no-op: the robot being
    # *registered* is the robot branch's question, being *online* is this one's.
    task = _task(require_network=False)
    assert _decide(task=task, robot=_robot(online=False)).decision == "dispatch"


# -- rules: unbounded programs ----------------------------------------------

def test_unbounded_program_without_max_duration_is_skipped():
    verdict = _decide(mission=_mission(duration_bounded=False), task=_task(max_duration_min=None))
    _skip(verdict, "dispatch_rule")
    assert "max_duration_min" in verdict.message


def test_unbounded_program_with_max_duration_dispatches():
    verdict = _decide(mission=_mission(duration_bounded=False), task=_task(max_duration_min=45))
    assert (verdict.decision, verdict.skip_reason, verdict.message) == ("dispatch", None, None)


# -- the four rules nothing can enforce -------------------------------------

def test_the_unenforceable_rules_are_read_and_ignored():
    # require_approval / block_if_maintenance_flagged / max_wind_kph /
    # require_safety_checklist have nothing behind them (spec 6.3). If they
    # blocked, a task would be skipped for a rule no operator can satisfy.
    task = _task(
        require_approval=True, block_if_maintenance_flagged=True, max_wind_kph=1.0,
        require_safety_checklist=True,
    )
    assert _decide(task=task).decision == "dispatch"


# -- the happy path ---------------------------------------------------------

def test_healthy_program_dispatches():
    verdict = _decide(task=_task(min_battery_pct=15), battery_pct=80)
    assert (verdict.decision, verdict.skip_reason, verdict.message) == ("dispatch", None, None)


def test_decide_does_not_read_the_clock_or_a_store():
    # Same inputs, same answer, and `now` is the only time there is.
    args = dict(occ=_occ(), task=_task(window_minutes=10), now=NOW + timedelta(minutes=11))
    assert _decide(**args) == _decide(**args)
