"""materialise(): TaskOccurrences from a task's schedule (tasks/scheduler.py).

Every expected `due_at` below is written out as a literal UTC string, worked
out by hand from the zone's offset, rather than computed with the same
zoneinfo calls the implementation uses -- a test that recomputes the answer
the way the code does can only ever agree with it.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from shared.schemas.tasks import MissionSchedule, MissionTask
from tasks.scheduler import materialise

SEOUL = "Asia/Seoul"           # UTC+9, no DST
NEW_YORK = "America/New_York"  # EST -5 / EDT -4; springs 2027-03-14, falls 2026-11-01


def _schedule(start_at, tz=SEOUL, recurrence="daily", **extra):
    return MissionSchedule(
        schedule_id="s-1", recurrence=recurrence, start_at=start_at, timezone=tz, **extra,
    )


def _task(schedule, task_id="t-1", **extra):
    return MissionTask(
        task_id=task_id, mission_id="m-1", site_id="site-a", robot_id="robot-1",
        schedule=schedule, created_by="operator-dev",
        created_at="2026-10-23T00:00:00Z", updated_at="2026-10-23T00:00:00Z", **extra,
    )


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _due(occurrences):
    return [o.due_at for o in occurrences]


def test_daily_schedule_produces_one_occurrence_per_day():
    # 09:00 Seoul is 00:00Z. `now` is 08:00 Seoul on the 23rd, so the 23rd's
    # run is still ahead and a 3-day horizon holds exactly three.
    task = _task(_schedule("2026-10-20T09:00"))
    out = materialise([task], _utc(2026, 10, 22, 23), horizon_days=3)

    assert _due(out) == [
        "2026-10-23T00:00:00Z", "2026-10-24T00:00:00Z", "2026-10-25T00:00:00Z",
    ]


def test_due_at_uses_the_schedules_timezone_not_utc():
    # Review Focus 1. 06:00 on the 23rd in Seoul is 21:00Z on the 22nd -- the
    # previous UTC day. Reading the wall clock as UTC would give 06:00Z.
    task = _task(_schedule("2026-10-20T06:00"))
    out = materialise([task], _utc(2026, 10, 22, 12), horizon_days=1)

    assert _due(out) == ["2026-10-22T21:00:00Z"]


def test_weekly_schedule_only_on_its_days():
    # 2026-10-26 is a Monday. Mon + Wed over 14 days from that Monday morning.
    task = _task(_schedule("2026-10-01T09:00", tz="UTC", recurrence="weekly", days_of_week=[0, 2]))
    out = materialise([task], _utc(2026, 10, 26, 8), horizon_days=14)

    assert _due(out) == [
        "2026-10-26T09:00:00Z", "2026-10-28T09:00:00Z",
        "2026-11-02T09:00:00Z", "2026-11-04T09:00:00Z",
    ]
    assert {datetime.fromisoformat(d.replace("Z", "+00:00")).weekday() for d in _due(out)} == {0, 2}


def test_weekly_weekday_is_the_local_weekday_not_the_utc_one():
    # Monday 06:00 in Seoul is Sunday 21:00Z. The schedule means Monday.
    task = _task(_schedule("2026-10-01T06:00", recurrence="weekly", days_of_week=[0]))
    out = materialise([task], _utc(2026, 10, 24), horizon_days=7)

    assert _due(out) == ["2026-10-25T21:00:00Z"]  # Sun 21:00Z == Mon 26th 06:00 Seoul


def test_weekly_with_no_days_materialises_nothing():
    task = _task(_schedule("2026-10-01T09:00", recurrence="weekly", days_of_week=[]))
    assert materialise([task], _utc(2026, 10, 23), horizon_days=14) == []


def test_past_once_schedule_materialises_nothing():
    # Review Focus 2.
    task = _task(_schedule("2026-10-16T09:00", recurrence="once"))
    assert materialise([task], _utc(2026, 10, 23), horizon_days=14) == []


def test_once_schedule_inside_the_horizon_materialises_exactly_once():
    task = _task(_schedule("2026-10-25T09:00", recurrence="once"))
    out = materialise([task], _utc(2026, 10, 23), horizon_days=14)

    assert _due(out) == ["2026-10-25T00:00:00Z"]


def test_once_schedule_beyond_the_horizon_is_not_yet_materialised():
    task = _task(_schedule("2026-12-25T09:00", recurrence="once"))
    assert materialise([task], _utc(2026, 10, 23), horizon_days=14) == []


def test_series_does_not_start_before_start_at():
    # A daily schedule that begins on the 25th produces nothing for the 23rd/24th.
    task = _task(_schedule("2026-10-25T09:00"))
    out = materialise([task], _utc(2026, 10, 22, 23), horizon_days=4)

    assert _due(out) == ["2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z"]


def test_until_stops_the_series():
    # `until` is the last date the schedule runs (spec), so it is inclusive:
    # 23rd and 24th, not the 25th.
    task = _task(_schedule("2026-10-20T09:00", tz="UTC", until="2026-10-24"))
    out = materialise([task], _utc(2026, 10, 23, 8), horizon_days=14)

    assert _due(out) == ["2026-10-23T09:00:00Z", "2026-10-24T09:00:00Z"]


def test_until_is_read_as_a_local_date():
    # until 2026-10-24 in Seoul still admits 06:00 on the 24th, which is
    # 21:00Z on the 23rd; the 25th (21:00Z on the 24th) is past it.
    task = _task(_schedule("2026-10-20T06:00", until="2026-10-24"))
    out = materialise([task], _utc(2026, 10, 22, 12), horizon_days=14)

    assert _due(out) == ["2026-10-22T21:00:00Z", "2026-10-23T21:00:00Z"]


def test_skip_dates_are_omitted():
    task = _task(_schedule("2026-10-20T09:00", tz="UTC", skip_dates=["2026-10-24"]))
    out = materialise([task], _utc(2026, 10, 23, 8), horizon_days=3)

    assert _due(out) == ["2026-10-23T09:00:00Z", "2026-10-25T09:00:00Z"]


def test_skip_dates_are_read_as_local_dates():
    # 2026-10-24 06:00 Seoul is 2026-10-23T21:00Z: a UTC-date comparison would
    # miss the skip and let the patrol run.
    task = _task(_schedule("2026-10-20T06:00", skip_dates=["2026-10-24"]))
    out = materialise([task], _utc(2026, 10, 22, 12), horizon_days=3)

    assert "2026-10-23T21:00:00Z" not in _due(out)
    assert _due(out) == ["2026-10-22T21:00:00Z", "2026-10-24T21:00:00Z"]


def test_dst_boundary_keeps_wall_clock_time():
    # US spring-forward is 2027-03-14. 09:00 New York is 14:00Z in EST (-5)
    # before it and 13:00Z in EDT (-4) after it: the wall clock holds, the UTC
    # instant moves an hour.
    task = _task(_schedule("2027-03-01T09:00", tz=NEW_YORK))
    out = materialise([task], _utc(2027, 3, 12), horizon_days=5)

    assert _due(out) == [
        "2027-03-12T14:00:00Z", "2027-03-13T14:00:00Z",   # EST
        "2027-03-14T13:00:00Z", "2027-03-15T13:00:00Z",   # EDT
        "2027-03-16T13:00:00Z",
    ]
    # Independent check of the headline claim: every one reads 09:00 locally.
    ny = ZoneInfo(NEW_YORK)
    for o in out:
        local = datetime.fromisoformat(o.due_at.replace("Z", "+00:00")).astimezone(ny)
        assert (local.hour, local.minute) == (9, 0)


def test_dst_fall_back_boundary_keeps_wall_clock_time():
    # US fall-back is 2026-11-01: 09:00 is 13:00Z (EDT) on the 31st, 14:00Z (EST) after.
    task = _task(_schedule("2026-10-01T09:00", tz=NEW_YORK))
    out = materialise([task], _utc(2026, 10, 31), horizon_days=3)

    assert _due(out) == [
        "2026-10-31T13:00:00Z", "2026-11-01T14:00:00Z", "2026-11-02T14:00:00Z",
    ]


def test_spring_forward_gap_wall_clock_runs_once_just_after_the_gap():
    # 02:30 does not exist in New York on 2027-03-14 (clocks go 02:00 -> 03:00).
    # It runs once, read at the pre-change offset (EST): 07:30Z == 03:30 EDT.
    # Not dropped, not doubled.
    task = _task(_schedule("2027-03-01T02:30", tz=NEW_YORK))
    out = materialise([task], _utc(2027, 3, 13), horizon_days=3)

    assert _due(out) == [
        "2027-03-13T07:30:00Z",   # 02:30 EST
        "2027-03-14T07:30:00Z",   # the gap: 03:30 EDT
        "2027-03-15T06:30:00Z",   # 02:30 EDT
    ]


def test_fall_back_repeated_wall_clock_runs_once_at_its_first_occurrence():
    # 01:30 happens twice in New York on 2026-11-01 (EDT then EST). The
    # patrol runs once, at the first: 05:30Z == 01:30 EDT. 06:30Z (the second
    # 01:30, EST) must not appear on that day.
    task = _task(_schedule("2026-10-01T01:30", tz=NEW_YORK))
    out = materialise([task], _utc(2026, 10, 31, 12), horizon_days=2)

    assert _due(out) == ["2026-11-01T05:30:00Z", "2026-11-02T06:30:00Z"]


def test_paused_and_archived_tasks_materialise_nothing():
    schedule = _schedule("2026-10-20T09:00")
    now = _utc(2026, 10, 22, 23)

    assert materialise([_task(schedule, status="paused")], now, horizon_days=3) == []
    assert materialise([_task(schedule, status="archived")], now, horizon_days=3) == []
    assert len(materialise([_task(schedule, status="enabled")], now, horizon_days=3)) == 3


def test_task_without_a_schedule_materialises_nothing():
    assert materialise([_task(None)], _utc(2026, 10, 23), horizon_days=14) == []


def test_occurrence_carries_the_tasks_identity_and_a_deterministic_id():
    task = _task(_schedule("2026-10-20T09:00"), task_id="patrol-7")
    (occ,) = materialise([task], _utc(2026, 10, 22, 23), horizon_days=1)

    assert occ.occurrence_id == "patrol-7-20261023T000000Z"
    assert (occ.task_id, occ.mission_id, occ.site_id, occ.robot_id) == (
        "patrol-7", "m-1", "site-a", "robot-1")
    assert occ.status == "scheduled"
    assert (occ.run_id, occ.skip_reason, occ.message) == (None, None, None)
    # Same input, same ids: re-materialising cannot double-book a slot.
    assert materialise([task], _utc(2026, 10, 22, 23), horizon_days=1) == [occ]


def test_unassigned_task_yields_unassigned_occurrences():
    task = _task(_schedule("2026-10-20T09:00")).model_copy(update={"robot_id": None})
    (occ,) = materialise([task], _utc(2026, 10, 22, 23), horizon_days=1)

    assert occ.robot_id is None


def test_occurrence_exactly_at_now_is_included_and_one_at_the_horizon_is_not():
    # The window is half-open, [now, now + horizon), matching the store's
    # bounded listing: a run due this very instant is still to be dispatched,
    # and one due exactly at the far edge belongs to the next tick's window.
    task = _task(_schedule("2026-10-20T09:00", tz="UTC"))
    out = materialise([task], _utc(2026, 10, 23, 9), horizon_days=2)

    assert _due(out) == ["2026-10-23T09:00:00Z", "2026-10-24T09:00:00Z"]


def test_past_occurrence_earlier_today_is_dropped():
    # A once-schedule, so tomorrow's run cannot appear and mask the result.
    task = _task(_schedule("2026-10-23T09:00", tz="UTC", recurrence="once"))
    out = materialise([task], _utc(2026, 10, 23, 9, 1), horizon_days=1)

    assert out == []


def test_zero_horizon_materialises_nothing():
    task = _task(_schedule("2026-10-20T09:00", tz="UTC"))
    assert materialise([task], _utc(2026, 10, 23, 8), horizon_days=0) == []


def test_naive_now_is_read_as_utc_and_aware_now_is_converted():
    task = _task(_schedule("2026-10-20T09:00", tz="UTC"))
    expected = ["2026-10-23T09:00:00Z"]

    assert _due(materialise([task], datetime(2026, 10, 23, 8), horizon_days=1)) == expected
    # 17:00 in Seoul is 08:00Z: the same instant.
    seoul_now = datetime(2026, 10, 23, 17, tzinfo=timezone(timedelta(hours=9)))
    assert _due(materialise([task], seoul_now, horizon_days=1)) == expected


def test_default_horizon_is_fourteen_days():
    task = _task(_schedule("2026-10-20T09:00", tz="UTC"))
    assert len(materialise([task], _utc(2026, 10, 23, 8))) == 14


def test_multiple_tasks_are_merged_in_due_at_order():
    late = _task(_schedule("2026-10-20T18:00", tz="UTC"), task_id="late")
    early = _task(_schedule("2026-10-20T07:00", tz="UTC"), task_id="early")
    out = materialise([late, early], _utc(2026, 10, 23, 6), horizon_days=2)

    assert [o.occurrence_id for o in out] == [
        "early-20261023T070000Z", "late-20261023T180000Z",
        "early-20261024T070000Z", "late-20261024T180000Z",
    ]


def test_a_task_with_an_unusable_schedule_does_not_stop_the_others():
    # The schema cannot validate that `timezone` is a real IANA name or that
    # `start_at` parses, so a bad stored task must not take the whole tick down.
    bad_zone = _task(_schedule("2026-10-20T09:00", tz="Mars/Olympus"), task_id="bad-zone")
    bad_start = _task(_schedule("not-a-time", tz="UTC"), task_id="bad-start")
    good = _task(_schedule("2026-10-20T09:00", tz="UTC"), task_id="good")
    out = materialise([bad_zone, bad_start, good], _utc(2026, 10, 23, 8), horizon_days=1)

    assert [o.task_id for o in out] == ["good"]


def test_window_edges_are_walked_in_the_schedules_local_dates():
    # The local date and the UTC date disagree for part of every day. West of
    # UTC, a run late on the local evening of "yesterday" (23:00 EDT on the
    # 22nd is 03:00Z on the 23rd) is already inside a window that opens on the
    # UTC 23rd; east of UTC, one early on the local morning after the window's
    # last UTC date (06:00 Seoul on the 25th is 21:00Z on the 24th) is too.
    west = _task(_schedule("2026-10-01T23:00", tz=NEW_YORK), task_id="west")
    east = _task(_schedule("2026-10-01T06:00"), task_id="east")

    assert _due(materialise([west], _utc(2026, 10, 23, 2), horizon_days=1)) == [
        "2026-10-23T03:00:00Z"]
    assert _due(materialise([east], _utc(2026, 10, 23, 23), horizon_days=1)) == [
        "2026-10-24T21:00:00Z"]


def test_start_date_is_compared_as_a_local_date_not_a_utc_one():
    # Seoul, series starts 2026-10-25 06:00. Its first run is 2026-10-24T21:00Z,
    # whose UTC date (the 24th) is *before* the start date. A UTC-date
    # comparison would wrongly call the 24th "before start" and suppress it.
    task = _task(_schedule("2026-10-25T06:00"))
    out = materialise([task], _utc(2026, 10, 24, 12), horizon_days=2)

    assert _due(out) == ["2026-10-24T21:00:00Z", "2026-10-25T21:00:00Z"]


def test_start_at_with_an_offset_or_trailing_junk_is_rejected_not_misread():
    # start_at is wall clock "YYYY-MM-DDTHH:mm" and nothing else; the schema
    # does not validate it, so this is the only check. A value carrying seconds
    # or an offset must be skipped loudly, never read as a wall-clock time with
    # the offset silently dropped -- that would run the patrol at the wrong instant.
    now = _utc(2026, 10, 23, 8)
    for bad in ("2026-10-20T09:00:00Z", "2026-10-20T09:00+09:00", "2026-10-20T09:00junk",
                "2026-10-20T09:00\n"):
        task = _task(_schedule(bad, tz="UTC"), task_id="bad")
        assert materialise([task], now, horizon_days=2) == [], bad
