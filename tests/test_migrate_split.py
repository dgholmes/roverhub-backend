"""One-shot migration of pre-split mission files: tasks/migrate_split.py.

The hazard under test: pydantic v2 ignores unknown keys, so a pre-split file's
`robot_id`, `schedule` and `dispatch_rule` -- unknown to MissionLibraryEntry
now -- would vanish without a trace if the file were parsed through it first.
Every assertion about them here is on the *contents* on the produced task.
"""
import json
import os

from shared.schemas.missions import MissionLibraryEntry
from shared.schemas.tasks import MissionTask
from tasks.migrate_split import SplitReport, split_missions
from tasks.store import TaskStore

NOW = "2026-10-23T10:00:00Z"
PROGRAM = '{"blocks":{}}'
SCHEDULE = {
    "schedule_id": "sch-m-1", "recurrence": "weekly", "start_at": "2026-10-22T06:00",
    "timezone": "Asia/Seoul", "days_of_week": [0, 2, 4], "window_minutes": 30,
}
RULE = {
    "min_battery_pct": 40.0, "require_network": False, "require_approval": True,
    "block_if_maintenance_flagged": False, "max_wind_kph": 25.0,
    "require_safety_checklist": True,
}
WAYPOINT = {"waypoint_id": "w1", "label": "A", "x_m": 1.0, "y_m": 2.0}


def _legacy(mission_id="m-1", **extra):
    """A mission as written before the split."""
    body = {
        "mission_id": mission_id, "name": "Legacy", "type": "patrol",
        "library_status": "scheduled", "creator": "MinJae Joo",
        "updated_at": "2026-10-22T02:23:44Z", "map_id": "map-1", "site_id": "site-a",
        "robot_id": "robot-7", "schedule": SCHEDULE, "dispatch_rule": RULE,
        "waypoints": [], "segments": [],
    }
    body.update(extra)
    return body


def _write(directory, body, name=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or body["mission_id"] + ".json")
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _run(tmp_path):
    return split_missions(
        str(tmp_path / "missions"), str(tmp_path / "tasks"), str(tmp_path / "out"), lambda: NOW
    )


def _mission(tmp_path, mission_id="m-1"):
    with open(tmp_path / "out" / f"{mission_id}.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_mission_with_a_schedule_becomes_a_mission_and_a_task(tmp_path):
    # robot_id, schedule and dispatch_rule land on the task; none remain on the mission
    # Review Focus 4: the contents are asserted, not just that a task exists.
    _write(tmp_path / "missions", _legacy())

    report = _run(tmp_path)

    assert (report.missions, report.tasks, report.skipped) == (1, 1, [])
    task = TaskStore(str(tmp_path / "tasks")).get_task("task-m-1")
    assert isinstance(task, MissionTask)
    assert task.mission_id == "m-1"
    assert task.site_id == "site-a"
    assert task.robot_id == "robot-7"
    assert task.schedule.model_dump() == {**SCHEDULE, "until": None, "skip_dates": []}
    assert task.dispatch_rule.model_dump() == RULE
    assert task.created_by == "MinJae Joo"
    assert task.created_at == task.updated_at == NOW
    assert task.version == 1
    assert task.status == "enabled"

    mission = _mission(tmp_path)
    for gone in ("robot_id", "schedule", "dispatch_rule", "library_status"):
        assert gone not in mission
    # The original mission's own stamp is kept; only the task is new.
    assert mission["updated_at"] == "2026-10-22T02:23:44Z"
    assert mission["version"] == 1
    assert MissionLibraryEntry.model_validate(mission).name == "Legacy"


def test_mission_without_a_schedule_produces_no_task(tmp_path):
    # but still produces a migrated mission
    body = _legacy()
    del body["schedule"]
    _write(tmp_path / "missions", body)

    report = _run(tmp_path)

    assert (report.missions, report.tasks, report.skipped) == (1, 0, [])
    assert TaskStore(str(tmp_path / "tasks")).list_tasks() == []
    assert _mission(tmp_path)["kind"] == "route"
    # robot_id had nowhere to go; the report says so rather than dropping it quietly.
    assert any("m-1" in n and "robot-7" in n for n in report.notes)


def test_a_null_schedule_is_the_same_as_none(tmp_path):
    _write(tmp_path / "missions", _legacy(schedule=None))
    report = _run(tmp_path)
    assert (report.missions, report.tasks) == (1, 0)


def test_kind_is_inferred_from_program_state(tmp_path):
    # program_state present -> "program"; absent -> "route"
    _write(tmp_path / "missions", _legacy("m-prog", program_state=PROGRAM))
    _write(tmp_path / "missions", _legacy("m-route", waypoints=[WAYPOINT]))

    report = _run(tmp_path)

    assert (report.missions, report.skipped) == (2, [])
    assert _mission(tmp_path, "m-prog")["kind"] == "program"
    assert _mission(tmp_path, "m-prog")["program_state"] == PROGRAM
    assert _mission(tmp_path, "m-route")["kind"] == "route"


def test_program_mission_waypoints_are_dropped(tmp_path):
    # the dead-reckoned route was fiction; it does not survive the migration
    _write(tmp_path / "missions", _legacy(
        program_state=PROGRAM, waypoints=[WAYPOINT], segments=[{"from": "w1", "to": "w2"}],
    ))

    report = _run(tmp_path)

    assert (report.missions, report.skipped) == (1, [])
    mission = _mission(tmp_path)
    assert mission["waypoints"] == [] and mission["segments"] == []
    assert any("m-1" in n and "waypoint" in n for n in report.notes)


def test_route_mission_keeps_its_waypoints(tmp_path):
    _write(tmp_path / "missions", _legacy(waypoints=[WAYPOINT]))
    _run(tmp_path)
    assert [w["waypoint_id"] for w in _mission(tmp_path)["waypoints"]] == ["w1"]


def test_unreadable_file_is_reported_not_fatal(tmp_path):
    # lands in report.skipped
    (tmp_path / "missions").mkdir()
    (tmp_path / "missions" / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "missions" / "list.json").write_text("[1, 2]", encoding="utf-8")
    _write(tmp_path / "missions", _legacy("m-ok"))

    report = _run(tmp_path)

    assert (report.missions, report.tasks) == (1, 1)
    assert len(report.skipped) == 2
    assert any("broken.json" in s for s in report.skipped)
    assert any("list.json" in s for s in report.skipped)


def test_a_file_that_would_lose_its_schedule_is_skipped_whole(tmp_path):
    # An invalid schedule must not yield a clean mission with the schedule gone.
    bad = {**SCHEDULE, "until": "2026-13-45"}
    _write(tmp_path / "missions", _legacy("m-bad", schedule=bad))

    report = _run(tmp_path)

    assert (report.missions, report.tasks) == (0, 0)
    assert any("m-bad" in s for s in report.skipped)
    assert not (tmp_path / "out" / "m-bad.json").exists()


def test_filename_must_match_mission_id(tmp_path):
    _write(tmp_path / "missions", _legacy("m-1"), name="other.json")
    report = _run(tmp_path)
    assert report.missions == 0
    assert any("other.json" in s for s in report.skipped)


def test_an_existing_task_is_never_overwritten(tmp_path):
    _write(tmp_path / "missions", _legacy())
    first = _run(tmp_path)
    assert first.tasks == 1
    path = tmp_path / "tasks" / "tasks" / "task-m-1.json"
    before = path.read_bytes()

    again = _run(tmp_path)

    assert (again.missions, again.tasks) == (0, 0)
    assert any("task-m-1" in s for s in again.skipped)
    assert path.read_bytes() == before


def test_originals_are_untouched(tmp_path):
    path = _write(tmp_path / "missions", _legacy())
    before = path.read_bytes()
    _run(tmp_path)
    assert path.read_bytes() == before
    assert sorted(os.listdir(tmp_path / "missions")) == ["m-1.json"]


def test_an_unusable_timezone_is_reported_but_still_migrated(tmp_path):
    # The schema does not check timezone (the router does on write); a legacy
    # file can carry one the scheduler cannot use. Migrate it, and say so.
    _write(tmp_path / "missions", _legacy(schedule={**SCHEDULE, "timezone": "Asia/Seol"}))
    report = _run(tmp_path)
    assert (report.missions, report.tasks) == (1, 1)
    assert any("Asia/Seol" in n for n in report.notes)


def test_non_json_files_in_the_directory_are_ignored(tmp_path):
    _write(tmp_path / "missions", _legacy())
    (tmp_path / "missions" / "README.txt").write_text("hi", encoding="utf-8")
    report = _run(tmp_path)
    assert (report.missions, report.skipped) == (1, [])


def test_missing_missions_directory_is_an_empty_run(tmp_path):
    assert _run(tmp_path) == SplitReport(missions=0, tasks=0, skipped=[], notes=[])


STALE_VALIDATION = {
    "status": "invalid",
    "issues": [{"severity": "error", "code": "noWaypoints", "message": "no waypoints", "waypoint_id": None}],
}
STORED_ESTIMATES = {"length_m": 12.5, "duration_s": 90.0, "energy_wh": None}


def test_program_estimates_are_reset_to_unbounded(tmp_path):
    # The scheduler's gate refuses an unbounded program with no max_duration_min.
    # A schema-default `duration_bounded: true` that nobody computed would walk
    # straight through it, so a migrated program asserts nothing until dryRun().
    _write(tmp_path / "missions", _legacy(program_state=PROGRAM, estimates=STORED_ESTIMATES))

    report = _run(tmp_path)

    assert _mission(tmp_path)["estimates"] == {
        "length_m": None, "duration_s": None, "energy_wh": None, "duration_bounded": False,
    }
    assert any("m-1" in n and "estimate" in n for n in report.notes)


def test_program_without_stored_estimates_is_also_unbounded(tmp_path):
    # The absent case is where the schema default (true) would have applied.
    _write(tmp_path / "missions", _legacy(program_state=PROGRAM))
    _run(tmp_path)
    assert _mission(tmp_path)["estimates"]["duration_bounded"] is False


def test_route_estimates_are_left_alone(tmp_path):
    # Recomputed from geometry wherever read, so a stored value is harmless.
    _write(tmp_path / "missions", _legacy(waypoints=[WAYPOINT], estimates=STORED_ESTIMATES))
    report = _run(tmp_path)
    estimates = _mission(tmp_path)["estimates"]
    assert (estimates["length_m"], estimates["duration_s"]) == (12.5, 90.0)
    assert estimates["duration_bounded"] is True
    assert not any("estimate" in n for n in report.notes)


def test_validation_is_reset_on_every_migrated_mission(tmp_path):
    # "no waypoints" was computed by a deleted compiler against dead-reckoned
    # waypoints; carrying it forward would present a stale claim as current.
    _write(tmp_path / "missions", _legacy("m-prog", program_state=PROGRAM, validation=STALE_VALIDATION))
    _write(tmp_path / "missions", _legacy("m-route", waypoints=[WAYPOINT], validation=STALE_VALIDATION))

    _run(tmp_path)

    for mission_id in ("m-prog", "m-route"):
        assert _mission(tmp_path, mission_id)["validation"] == {"status": "unvalidated", "issues": []}
