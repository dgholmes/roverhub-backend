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

This is only the arithmetic. Whether a due occurrence is dispatched, skipped or
missed its window is the dispatch gate's job, and persisting them is the
store's.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, List
from zoneinfo import ZoneInfo

from shared.schemas.tasks import MissionSchedule, MissionTask, TaskOccurrence

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
    start_date, start_time = _parse_start(schedule.start_at)
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


def _parse_start(start_at: str) -> tuple[date, time]:
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
