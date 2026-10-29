"""Materialise task occurrences from schedules (2026-10-23).

`materialise()` turns the site's `MissionTask`s into the concrete
`TaskOccurrence`s that fall due in a window starting now. It is a pure
function -- tasks and a clock reading in, occurrences out -- with no store, no
clock of its own and no I/O. That is deliberate: the interesting failures here
are all about calendars (a wall-clock time that does not exist, or happens
twice, on a DST night), and they are only testable if `now` is a parameter.

A schedule stores a *wall-clock* time plus an IANA zone name, never an offset,
so a 09:00 patrol stays at 09:00 across a daylight-saving change. Everything
about *which day* a run belongs to (start, `until`, `skip_dates`, the weekday
of a weekly schedule) is therefore decided on the schedule's own local date;
only at the very end is the local time converted to the UTC instant that
`due_at` carries.

`materialise()` is only the arithmetic. Whether a due occurrence is dispatched
or skipped is `decide()`'s job, and what to do about it each tick is
`SchedulerLoop`'s; persisting occurrences is the store's.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable, List, Literal, NamedTuple, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from missions.router import MissionStore
from robots.registry import RobotRecord, RobotRegistry
from runs.store import RunStore
from shared.schemas.missions import MissionKind, MissionLibraryEntry
from shared.schemas.runs import MissionRun
from shared.schemas.tasks import (
    MissionSchedule, MissionTask, SkipReason, TaskOccurrence,
)
from tasks.store import TaskStore, parse_utc

logger = logging.getLogger("roverhub.tasks")

# "YYYY-MM-DDTHH:mm", wall clock, no offset (MissionSchedule.start_at). The
# schema types it as a plain str, so this is the only place it is ever checked.
# Matched with fullmatch: a value carrying seconds or an offset ("...:00Z",
# "+09:00") must be rejected, not read as a wall clock with the offset dropped.
_START_AT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})")


def materialise(
    tasks: Iterable[MissionTask], now: datetime, horizon_days: int = 14,
) -> List[TaskOccurrence]:
    """Every occurrence due in the half-open window `[now, now + horizon_days)`.

    The window is in elapsed time, not calendar days, and half-open like the
    store's bounded listing: a run due this very instant is still ahead of us,
    one due exactly at the far edge belongs to the next tick's window, and one
    already in the past is never produced -- a late patrol is the dispatch
    gate's `window_missed` decision, not something to invent here.

    `now` that is naive is read as UTC, the convention for every `due_at`.

    A task that is not `enabled`, has no schedule, or has a schedule that
    cannot be evaluated (unknown timezone, unparseable `start_at` -- neither is
    checkable by the schema) yields nothing; the last is logged, and never
    stops the other tasks, because one bad record must not stop every patrol on
    the site.

    Output is sorted by `due_at`, then `occurrence_id`, the order the store
    lists in. Ids are deterministic (`<task_id>-<compact UTC due_at>`), so
    materialising the same schedule twice cannot double-book a slot.
    """
    window_start = _as_utc(now)
    window_end = window_start + timedelta(days=horizon_days)

    out: List[TaskOccurrence] = []
    for task in tasks:
        if task.status != "enabled" or task.schedule is None:
            continue
        try:
            due_times = _due_times(task.schedule, window_start, window_end)
        except (ValueError, KeyError, OSError) as exc:
            # ZoneInfo raises ZoneInfoNotFoundError (a KeyError) for an unknown
            # name and ValueError for a malformed one; date/time construction
            # raises ValueError for a start_at such as "2026-13-45T09:00".
            logger.warning(
                "task %s: schedule %s cannot be evaluated, no occurrences: %s",
                task.task_id, task.schedule.schedule_id, exc,
            )
            continue
        for due in due_times:
            out.append(TaskOccurrence(
                occurrence_id=f"{task.task_id}-{due:%Y%m%dT%H%M%SZ}",
                task_id=task.task_id,
                mission_id=task.mission_id,
                site_id=task.site_id,
                robot_id=task.robot_id,
                due_at=f"{due:%Y-%m-%dT%H:%M:%SZ}",
            ))
    out.sort(key=lambda o: (o.due_at, o.occurrence_id))
    return out


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _due_times(
    schedule: MissionSchedule, window_start: datetime, window_end: datetime,
) -> List[datetime]:
    """UTC instants in `[window_start, window_end)` at which `schedule` runs."""
    start_date, start_time = parse_start(schedule.start_at)
    zone = ZoneInfo(schedule.timezone)
    skip = set(schedule.skip_dates)

    # The local dates the window touches. Every due time in the window has a
    # local date in this range, so nothing needs padding on either side.
    first = window_start.astimezone(zone).date()
    last = window_end.astimezone(zone).date()

    due_times: List[datetime] = []
    day = first
    while day <= last:
        if _runs_on(schedule, start_date, day) and not _excluded(schedule, skip, day):
            due = _to_utc(day, start_time, zone)
            if window_start <= due < window_end:
                due_times.append(due)
        day += timedelta(days=1)
    return due_times


def parse_start(start_at: str) -> tuple[date, time]:
    match = _START_AT_RE.fullmatch(start_at)
    if match is None:
        raise ValueError(f"start_at {start_at!r} is not YYYY-MM-DDTHH:mm")
    return (
        date.fromisoformat(match.group(1)),
        time(int(match.group(2)), int(match.group(3))),
    )


def _runs_on(schedule: MissionSchedule, start_date: date, day: date) -> bool:
    """The recurrence rule, as the frontend's `runsOn()` states it: `once` is
    the start date only, `daily` every date from it, `weekly` the dates from it
    whose weekday (Monday=0) is in `days_of_week`."""
    if day < start_date:
        return False
    if schedule.recurrence == "once":
        return day == start_date
    if schedule.recurrence == "daily":
        return True
    # Only "weekly" remains: ScheduleRecurrence is Literal["once", "daily",
    # "weekly"] (shared/schemas/tasks.py), so pydantic rejects anything else.
    return day.weekday() in schedule.days_of_week


def _excluded(schedule: MissionSchedule, skip: set, day: date) -> bool:
    # `until` is the last date the schedule runs, so inclusive. Both it and
    # `skip_dates` are local dates in the schedule's zone (schema comment).
    iso = day.isoformat()
    return iso in skip or (schedule.until is not None and iso > schedule.until)


def _to_utc(day: date, wall_clock: time, zone: ZoneInfo) -> datetime:
    """Local wall clock -> UTC instant, with `fold=0` made explicit.

    Two nights a year the wall clock is not one-to-one with instants, and the
    schedule is promised to run *once* either way:

      * Spring forward, the time does not exist (02:30 on a night the clocks go
        02:00 -> 03:00). With `fold=0` it is read at the pre-change offset, so
        it runs at the equivalent instant just after the gap (03:30 local).
        Skipping it would silently lose a patrol.
      * Fall back, the time happens twice (01:30 twice). With `fold=0` it is the
        first one, so it runs once, not twice.
    """
    local = datetime.combine(day, wall_clock, tzinfo=zone).replace(fold=0)
    return local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# The dispatch gate
# ---------------------------------------------------------------------------

Decision = Literal["dispatch", "skip"]

# The mission kinds something on the edge can actually receive. Configuration,
# not logic. The edge's MissionExecutor runs programs (START_MISSION); the SLAM
# backpack will add "route". Do not hard-code "route skips, program dispatches"
# in decide(): a kind is dispatched only when it is listed here.
EXECUTOR_KINDS: frozenset[MissionKind] = frozenset({"program"})


class Verdict(NamedTuple):
    """What the gate decided and, for a skip, why.

    `decision` and `skip_reason` are the pair spec 6.1 describes. `message` is
    what makes the skip answerable: `skip_reason` is a closed set shared with
    the UI, and "dispatch_rule" alone cannot say *which* rule, so the operator
    asking "why didn't my patrol run?" would be left guessing. It is None for a
    dispatch.
    """

    decision: Decision
    skip_reason: Optional[SkipReason]
    message: Optional[str]


def _skip(reason: SkipReason, message: str) -> Verdict:
    return Verdict("skip", reason, message)


def decide(
    occ: TaskOccurrence,
    task: MissionTask,
    mission: Optional[MissionLibraryEntry],
    robot: Optional[RobotRecord],
    now: datetime,
    executor_kinds: frozenset[MissionKind] = EXECUTOR_KINDS,
    *,
    battery_pct: Optional[float] = None,
    robot_busy: bool = False,
    unattended_enabled: bool = False,
) -> Verdict:
    """Dispatch or skip one occurrence that has come due. Pure: no store, no
    clock, no I/O -- everything it judges is a parameter.

    The order is spec 6.1's and it matters, because the first failing check is
    the reason the operator is shown: window, then executor, then robot, then
    rules. A patrol that is 3 hours late AND has no executor is, to the person
    reading the log, a missed patrol first.

    `robot` is the registry's record for `occ.robot_id`, None if it has none.
    `battery_pct` is a parameter and not read from `robot` because the cloud
    does not have it: `RobotRecord` carries no battery field and the MQTT client
    uses telemetry only to bump `last_seen`, so today the tick passes None.
    `robot_busy` is whether that robot already has a dispatched or running
    occurrence; only the tick, which sees every occurrence, can know.
    """
    now = _as_utc(now)

    # 1. Window. Past it, the patrol that was asked for is gone; starting it
    # late would be a different patrol.
    window = task.schedule.window_minutes if task.schedule is not None else None
    if window is not None:
        due = parse_utc(occ.due_at)
        if due is None:
            # Fail closed: a window that cannot be measured has not been met.
            return _skip("window_missed", (
                f"due_at {occ.due_at!r} is not a valid time, so the {window}-minute "
                f"window cannot be checked"
            ))
        late = now - due
        if late > timedelta(minutes=window):
            # Minutes and seconds: whole minutes alone would print "30 min past
            # its due time; the window is 30 min" for a patrol 30 min 30 s late.
            minutes, seconds = divmod(int(late.total_seconds()), 60)
            return _skip("window_missed", (
                f"{minutes} min {seconds} s past its due time; the window is {window} min"
            ))

    # 2. Executor for this mission's kind.
    if mission is None:
        # Its own reason: "no_executor" would tell the operator the patrol is
        # waiting on the edge when the real cause is the mission. "Missing or
        # unreadable", not "deleted": MissionStore.get returns None for a file
        # that fails validation (one written before `kind` existed) as well as
        # for an absent one, and the message must not blame a deletion.
        return _skip("mission_missing", (
            f"mission {occ.mission_id!r} is missing or unreadable in the mission "
            f"library, so there is nothing to run"
        ))
    if mission.kind not in executor_kinds:
        return _skip("no_executor", (
            f"nothing on the edge can run a {mission.kind} mission yet"
        ))
    # Directly after the executor check, so a kind nothing can run is still
    # reported as no_executor. Scheduled runs have nobody watching, and E-Stop has
    # not yet been tested on a moving robot.
    if not unattended_enabled:
        return _skip(
            "unattended_disabled",
            "Scheduled runs switch on after E-Stop has been tested on a moving robot.",
        )

    # 3. Robot: unset, unreachable (unknown to the registry, or offline), or
    # already running something. Reachability is unconditional: a command
    # published to a robot nobody can reach goes into the void, and no task
    # setting may opt out of that.
    if occ.robot_id is None:
        return _skip("robot_unavailable", "no robot is assigned to this task")
    if robot is None:
        return _skip("robot_unavailable", (
            f"robot {occ.robot_id!r} is not registered with the cloud"
        ))
    if not robot.online:
        return _skip("robot_unavailable", (
            f"robot {occ.robot_id!r} is offline"
        ))
    if robot_busy:
        return _skip("robot_busy", (
            f"robot {occ.robot_id!r} is already running another scheduled mission"
        ))

    # 4. Dispatch rules (spec 6.3). Only min_battery_pct is consulted. The rest
    # are read and ignored on purpose:
    #   - require_network is not unenforceable but subsumed: branch 3 already
    #     skips every unreachable robot, so by here the robot is reachable
    #     whatever the task says.
    #   - require_approval, block_if_maintenance_flagged, max_wind_kph and
    #     require_safety_checklist have nothing behind them (no approval flow,
    #     maintenance flag, weather source or checklist exist), so enforcing
    #     them would skip tasks for a rule no operator can satisfy.
    rule = task.dispatch_rule
    # `> 0`, not "is set": 0 is the project's explicit no-gate opt-out (the
    # edge's SAFETY_MIN_BATTERY_PCT=0), and a gRPC-only host has no battery
    # reading at all. Any real threshold with no reading is a skip.
    if rule.min_battery_pct is not None and rule.min_battery_pct > 0:
        # NaN is "unknown" too: `nan < 30` is False, so it would otherwise pass.
        if battery_pct is None or math.isnan(battery_pct):
            return _skip("dispatch_rule", (
                # About the argument, not the cloud: this is true of any robot
                # without a reading, today and after the registry carries one.
                f"min_battery_pct rule ({rule.min_battery_pct:g}%): the battery "
                f"reading is unavailable for this robot, so the rule cannot be checked"
            ))
        if battery_pct < rule.min_battery_pct:
            return _skip("dispatch_rule", (
                f"min_battery_pct rule: battery is {battery_pct:g}%, below the "
                f"required {rule.min_battery_pct:g}%"
            ))
    if not mission.estimates.duration_bounded and task.max_duration_min is None:
        # An unbounded program has no computable duration, and the robot would
        # be held for as long as it runs. The task must cap it.
        return _skip("dispatch_rule", (
            "this mission has no computable duration (an unbounded wait or loop) "
            "and the task sets no max_duration_min"
        ))

    return Verdict("dispatch", None, None)


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

# Invoked as publish(occurrence, run) after the MissionRun exists; api/app.py's
# `_publish_scheduled` sends START_MISSION. Sync, and run on a worker thread with
# the rest of the tick. Raising marks the occurrence failed with the message.
Publish = Callable[[TaskOccurrence, MissionRun], None]


def _battery_pct(robot: Optional[RobotRecord]) -> Optional[float]:
    """The robot's battery as the cloud knows it -- which is not at all.

    `RobotRecord` has no battery field and `realtime/mqtt_client.py` reads a
    telemetry frame only to bump `last_seen`. So this is None for every robot,
    and a task carrying a `min_battery_pct` above 0 always skips (safe, and visible: the
    skip message says the reading is unavailable). Retaining the last telemetry
    battery per robot is separate work, on the backlog; when it lands, this is
    the one place that changes.
    """
    return None


def _iso(moment: datetime) -> str:
    return f"{moment:%Y-%m-%dT%H:%M:%SZ}"


class _RepeatFilter(logging.Filter):
    """Let an identical warning through once per `quiet_s`, not once per tick.

    The tick re-reads the same tasks every 30 s, so one task with an unknown
    timezone would otherwise log the same warning 2,880 times a day, burying
    every other warning. A warning is the only signal an operator gets that a
    patrol will never run, so it is repeated, but hourly, not constantly.
    Keyed on the formatted message, plus the exception's type and text when the
    record carries one. The message alone is not enough: `logger.exception(
    "scheduler tick failed")` has a constant message, so after the first
    failure every later, *different* one (disk full, then a corrupt store file)
    would be hidden for an hour along with its traceback. Including the
    exception, rather than passing every `exc_info` record through, keeps a
    persistent failure from tracebacking every 30 s while still showing each new
    one. The exception's text, not the object, because `materialise()` and the
    tick raise a fresh instance each time, so the log record's args never
    compare equal.

    Scope: attached to the `roverhub.tasks` logger, which `tasks/store.py` also
    logs to, so identical store warnings raised on API request threads are
    throttled too while the loop runs. That is acceptable (they repeat the same
    way, per call) but it is a side effect worth knowing.
    """

    def __init__(self, quiet_s: float = 3600.0, clock: Callable[[], float] = _time.monotonic):
        super().__init__()
        self._quiet_s = quiet_s
        self._clock = clock
        self._last: dict = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        exc = record.exc_info[1] if record.exc_info else None
        key = (
            record.levelno, record.getMessage(),
            type(exc).__name__ if exc is not None else None,
            str(exc) if exc is not None else None,
        )
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._quiet_s:
            return False
        self._last[key] = now
        return True


class SchedulerLoop:
    """Every `interval_s`: materialise occurrences into the store, then decide
    each one that has come due and write the outcome back.

    `materialise()` and `decide()` carry the logic and are tested on their own;
    this is the I/O around them, and `run_once()` is the tick without the sleep.
    api/app.py starts it at startup (unless SCHEDULER_ENABLED turns it off) and
    cancels it at shutdown.
    """

    def __init__(
        self,
        store: TaskStore,
        registry: RobotRegistry,
        mission_store: MissionStore,
        publish: Publish,
        interval_s: float = 30,
        *,
        run_store: RunStore,
        executor_kinds: frozenset[MissionKind] = EXECUTOR_KINDS,
        unattended_enabled: bool = False,
    ):
        self._unattended_enabled = unattended_enabled
        self._store = store
        self._registry = registry
        self._missions = mission_store
        self._publish = publish
        self._interval_s = interval_s
        self._runs = run_store
        self._executor_kinds = executor_kinds

    async def run(self) -> None:
        # Scoped to this loop's lifetime, so building a loop (in a test) does
        # not leave a filter on a logger every other module shares.
        repeats = _RepeatFilter()
        logger.addFilter(repeats)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.run_once, datetime.now(timezone.utc))
                except Exception:
                    # One bad tick must not end scheduling for good.
                    logger.exception("scheduler tick failed")
                await asyncio.sleep(self._interval_s)
        finally:
            logger.removeFilter(repeats)

    def run_once(self, now: datetime) -> None:
        now = _as_utc(now)
        self._prune_orphans()
        self._drop_stale_plans(now)
        self._materialise(now)

        everything = self._store.list_occurrences()
        # Robots with a mission in flight. Updated as this tick dispatches, so
        # two occurrences due at the same moment cannot both claim one robot.
        busy = {
            o.robot_id for o in everything
            if o.status in ("dispatched", "running") and o.robot_id is not None
        }
        for occ in everything:  # due_at order, as the store lists
            if occ.status not in ("scheduled", "due"):
                continue
            due = parse_utc(occ.due_at)
            if due is None:
                logger.warning(
                    "occurrence %s: due_at %r is not a valid time, leaving it alone",
                    occ.occurrence_id, occ.due_at,
                )
                continue
            if due > now:
                continue
            try:
                self._handle(occ, now, busy)
            except Exception:
                # One bad record must not stop the other patrols on the site.
                logger.exception("occurrence %s: could not be handled", occ.occurrence_id)

    def _prune_orphans(self) -> None:
        # Before materialising, so the queue the rest of the tick reads is
        # already clean. The policy (what counts as an orphan, and what is
        # history that must stay) lives in TaskStore.prune_orphan_occurrences.
        try:
            pruned = self._store.prune_orphan_occurrences()
        except Exception:
            # Housekeeping must not stop the patrols that are due.
            logger.exception("could not prune orphaned occurrences")
            return
        if pruned:
            # WARNING, not INFO: nothing configures logging, so INFO is dropped,
            # and a deletion the scheduler decided on its own must be visible.
            logger.warning(
                "pruned %d never-started occurrence(s) whose task no longer exists: %s",
                len(pruned), pruned,
            )

    def _drop_stale_plans(self, now: datetime) -> None:
        # A planned run that the task, as it is now, would not plan: worked out
        # before an edit moved the time, changed the robot or dropped the
        # schedule. Saving a task already re-plans it (router._replan); this
        # catches an edit made any other way, and runs planned before that
        # existed. Only enabled tasks: a paused task's runs are cancelled when
        # they come due, which is how the calendar says it was paused.
        try:
            planned: dict = {}
            for occ in self._store.list_occurrences():
                due = parse_utc(occ.due_at)
                if occ.status == "scheduled" and occ.run_id is None and due is not None and due >= now:
                    planned.setdefault(occ.task_id, []).append(occ)
            stale = []
            for task in self._store.list_tasks():
                if task.status != "enabled" or task.task_id not in planned:
                    continue
                wanted = {o.occurrence_id: o for o in materialise([task], now)}
                for occ in planned[task.task_id]:
                    want = wanted.get(occ.occurrence_id)
                    if want is None or want.robot_id != occ.robot_id or want.mission_id != occ.mission_id:
                        self._store.delete_occurrence(occ.occurrence_id)
                        stale.append(occ.occurrence_id)
        except Exception:
            logger.exception("could not drop stale planned occurrences")
            return
        if stale:
            logger.warning("dropped %d planned occurrence(s) their task no longer plans: %s", len(stale), stale)

    def _materialise(self, now: datetime) -> None:
        for occ in materialise(self._store.list_tasks(), now):
            # Never overwrite: once written, an occurrence is the store's, and
            # one that has left "scheduled" must keep its outcome.
            if self._store.get_occurrence(occ.occurrence_id) is None:
                self._store.put_occurrence(occ)

    def _mission(self, mission_id: str) -> Optional[MissionLibraryEntry]:
        try:
            return self._missions.get(mission_id)
        except HTTPException:  # a mission_id the library rejects as unsafe
            return None

    def _handle(self, occ: TaskOccurrence, now: datetime, busy: set) -> None:
        task = self._store.get_task(occ.task_id)
        if task is None or task.status != "enabled":
            # Paused, archived or deleted after this was materialised: it is
            # not a patrol anyone still wants, and not a skip for a reason the
            # operator can act on, so it is cancelled rather than dispatched.
            self._store.put_occurrence(occ.model_copy(update={
                "status": "cancelled",
                "message": "the task was deleted" if task is None
                else f"the task is {task.status}",
            }))
            return

        robot = self._registry.get(occ.robot_id) if occ.robot_id is not None else None
        mission = self._mission(occ.mission_id)
        verdict = decide(
            occ, task, mission, robot, now, self._executor_kinds,
            battery_pct=_battery_pct(robot), robot_busy=occ.robot_id in busy,
            unattended_enabled=self._unattended_enabled,
        )
        if verdict.decision == "skip":
            self._store.put_occurrence(occ.model_copy(update={
                "status": "skipped",
                "skip_reason": verdict.skip_reason,
                "message": verdict.message,
            }))
            return
        # A dispatch that could not be sent leaves the robot free for the next.
        if self._dispatch(occ, mission, now):
            busy.add(occ.robot_id)

    def _dispatch(self, occ: TaskOccurrence, mission: MissionLibraryEntry, now: datetime) -> bool:
        # The occurrence id is the run id, so two attempts could never produce
        # two runs. Nothing retries: run_once revisits only "scheduled" and "due"
        # occurrences, so this runs at most once per occurrence.
        robot = self._registry.get(occ.robot_id) if occ.robot_id is not None else None
        run = MissionRun(
            run_id=occ.occurrence_id, mission_id=occ.mission_id, mission=mission,
            robot_id=occ.robot_id, site_id=occ.site_id, started_at=_iso(now),
            started_by="schedule", source="schedule",
            # So a bridge restart fails this run like any other (runs/reconcile).
            bridge_boot_id=robot.bridge_boot_id if robot is not None else None,
        )
        # "dispatched" is written before anything is sent, so a failure mid-way
        # leaves a record of the attempt rather than a store that says it never
        # tried. The cost, stated plainly: there is no recovery. If `runs.put`
        # (or the process) fails after this write, the occurrence stays
        # "dispatched" with no run, `run_once` seeds `busy` from "dispatched",
        # and that robot is skipped as `robot_busy` from then on. Only a publish
        # failure is cleaned up (below). Nothing can reach this while
        # EXECUTOR_KINDS is empty; reaping stuck dispatches belongs with the
        # executor's dispatched/running -> terminal lifecycle (Plan 3).
        dispatched = occ.model_copy(update={"status": "dispatched", "run_id": run.run_id})
        self._store.put_occurrence(dispatched)
        self._runs.put(run)
        try:
            self._publish(dispatched, run)
        except Exception as exc:
            logger.exception("occurrence %s: publish failed", occ.occurrence_id)
            self._runs.put(run.model_copy(update={
                "result": "failed", "ended_at": _iso(now), "end_reason": f"publish failed: {exc}",
            }))
            self._store.put_occurrence(dispatched.model_copy(update={
                "status": "failed", "message": f"the dispatch could not be sent: {exc}",
            }))
            return False
        self._store.put_occurrence(dispatched.model_copy(update={"status": "running"}))
        return True


def has_spoken_steps(steps) -> bool:
    """True if any command step, however deeply nested in repeat / repeatUntil /
    if bodies, speaks (carries `speech`, or is a SPEAK command)."""
    for step in steps:
        kind = getattr(step, "kind", None)
        if kind == "command":
            if step.speech is not None or step.type == "SPEAK":
                return True
        elif kind in ("repeat", "repeatUntil"):
            if has_spoken_steps(step.body):
                return True
        elif kind == "if":
            if has_spoken_steps(step.then) or has_spoken_steps(step.else_):
                return True
    return False
