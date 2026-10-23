"""One-shot migration of pre-split mission files (2026-10-23).

Before the split, one `MissionLibraryEntry` carried `robot_id`, `schedule`,
`dispatch_rule` and `library_status` alongside the authored route or program.
Those now live on a `MissionTask` (shared/schemas/tasks.py), and a mission
also needs a `kind` and a `version`. A file written the old way no longer
validates, so the mission library skips it and the operator's work vanishes
from the console. This reads those files and writes the new shape.

    python -m tasks.migrate_split --missions data/missions --out data/missions.split --tasks data/tasks

The files are read as RAW DICTS with `json.load`, never through
`MissionLibraryEntry`. pydantic v2 ignores unknown keys, and `robot_id`,
`schedule` and `dispatch_rule` are unknown to the model now: parsing through it
first would discard exactly the data being migrated and yield a clean-looking
mission with no task beside it -- no error, no warning, the schedule just gone.

Writes missions to `out_dir` and tasks to `tasks_dir`; it never modifies or
deletes anything in `missions_dir`. Swapping the directories is a separate,
manual step for after the console is seen reading the new shape.

Each file is all-or-nothing: if its task cannot be built, the mission is not
written either, since a mission without its schedule is the silent loss above.

Reruns are safe. Neither output is ever overwritten: a file whose mission (and
task, if scheduled) already exist is reported as `already_migrated`, a missing
half of an interrupted migration is written, and a task that is not this
mission's own is refused. To redo a migration, delete its output first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from api.common import now_iso
from missions.router import MISSION_ID_RE, MissionStore, SUFFIX
from shared.schemas.missions import MissionLibraryEntry
from shared.schemas.tasks import DispatchRule, MissionTask
from tasks.scheduler import parse_start
from tasks.store import TASK_ID_RE, TaskStore

# The four fields that moved to MissionTask (`library_status` has no successor:
# "completed"/"failed" describe a run, and nothing could ever set them on a mission).
MOVED_FIELDS = ("robot_id", "schedule", "dispatch_rule", "library_status")


@dataclass
class SplitReport:
    missions: int = 0
    tasks: int = 0
    # Files that produced nothing, each as "<file>: <why>". One bad file never
    # costs the others.
    skipped: List[str] = field(default_factory=list)
    # Files that migrated but where something was dropped or may not work, each
    # as "<mission_id>: <what>". These are what a human should look at.
    notes: List[str] = field(default_factory=list)
    # Files whose output (mission and, if scheduled, task) was already complete,
    # so this run left them alone. Kept apart from `skipped` so that a rerun that
    # did nothing because it was done cannot be mistaken for one that hit
    # corrupt files, and callers need not parse a message to tell.
    already_migrated: List[str] = field(default_factory=list)


def task_id_for(mission_id: str) -> str:
    return f"task-{mission_id}"


def _unusable_schedule(schedule) -> Optional[str]:
    """Why the scheduler could not use this schedule, or None. The schema leaves
    `timezone` and `start_at` to the router, so a legacy file can carry values
    the scheduler would skip over; that is reported, not grounds to refuse."""
    try:
        ZoneInfo(schedule.timezone)
    except Exception:  # ZoneInfoNotFoundError, ValueError for malformed keys
        return f"timezone {schedule.timezone!r} is not a known IANA zone"
    try:
        parse_start(schedule.start_at)
    except ValueError:
        return f"start_at {schedule.start_at!r} is not YYYY-MM-DDTHH:mm"
    return None


def _migrate_one(raw: dict, stem: str, now: str):
    """(mission, task-or-None, notes) for one raw legacy dict. Raises ValueError
    with a reason when it cannot be migrated whole. Touches nothing on disk; the
    notes are the caller's to report only if the file is actually written."""
    mission_id = raw.get("mission_id")
    if mission_id != stem:
        raise ValueError(f"mission_id is {mission_id!r}, which does not match the file name")
    if not MISSION_ID_RE.match(mission_id):
        raise ValueError(f"mission_id {mission_id!r} is not a valid id")

    body = dict(raw)
    moved = {name: body.pop(name, None) for name in MOVED_FIELDS}
    notes: List[str] = []

    # Present-or-absent, not truthiness: an empty-string program is still a program.
    body.setdefault("kind", "program" if body.get("program_state") is not None else "route")
    # Both routers store a create at version 1, so migrated records start there.
    body.setdefault("version", 1)
    if body["kind"] == "program" and (body.get("waypoints") or body.get("segments")):
        notes.append(
            f"{mission_id}: dropped {len(body.get('waypoints') or [])} waypoint(s) and "
            f"{len(body.get('segments') or [])} segment(s) from a program mission "
            "(dead-reckoned by the old compiler, not authored)"
        )
        body["waypoints"] = []
        body["segments"] = []

    # The stored validation was computed by a compiler this branch deletes, over
    # waypoints that were themselves dead-reckoned: a stale claim ("no
    # waypoints") that would read as current until someone re-saves the mission.
    # "Not checked yet" is the true statement, and it needs no new value invented.
    body["validation"] = {"status": "unvalidated", "issues": []}
    if body["kind"] == "program":
        # `duration_bounded` defaults to true, but nobody computed it for this
        # program. tasks.scheduler.decide() relies on it to refuse an unbounded
        # program that has no max_duration_min, so a migrated one must not walk
        # through that gate on a default: refuse rather than permit, until the
        # console's dry run computes the real figure. Route estimates are
        # recomputed from geometry wherever read, so they are left alone.
        body["estimates"] = {
            "length_m": None, "duration_s": None, "energy_wh": None, "duration_bounded": False,
        }
        notes.append(
            f"{mission_id}: estimates reset to unbounded (duration_bounded=false) -- the stored "
            "figures were not computed for this program; opening it in the console re-runs the dry run"
        )

    ignored = sorted(set(body) - set(MissionLibraryEntry.model_fields))
    if ignored:
        notes.append(f"{mission_id}: unknown keys not carried over: {', '.join(ignored)}")
    mission = MissionLibraryEntry.model_validate(body)

    task = None
    if moved["schedule"]:
        task_id = task_id_for(mission_id)
        # "task-" + a 124..128 character mission id passes MISSION_ID_RE but not
        # the task store's identical 128 limit, whose get_task would raise an
        # HTTPException (not a ValueError) and end the whole run.
        if not TASK_ID_RE.match(task_id):
            raise ValueError(f"task id {task_id!r} is longer than 128 characters or otherwise invalid")
        task = MissionTask.model_validate({
            "task_id": task_id,
            "mission_id": mission_id,
            "site_id": mission.site_id,
            "robot_id": moved["robot_id"],
            "schedule": moved["schedule"],
            # An absent rule takes the schema's refuse-by-default one.
            **({"dispatch_rule": moved["dispatch_rule"]} if moved["dispatch_rule"] else {}),
            "created_by": mission.creator,
            "created_at": now,
            "updated_at": now,
        })
        problem = _unusable_schedule(task.schedule)
        if problem:
            notes.append(f"{mission_id}: migrated, but the scheduler cannot use its schedule: {problem}")
    else:
        # No schedule means no task, so these have nowhere to live. Say so.
        if moved["robot_id"]:
            notes.append(f"{mission_id}: robot_id {moved['robot_id']!r} not carried over (no schedule, so no task)")
        if moved["dispatch_rule"] and DispatchRule.model_validate(moved["dispatch_rule"]) != DispatchRule():
            notes.append(f"{mission_id}: non-default dispatch_rule not carried over (no schedule, so no task)")

    return mission, task, notes


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _same_assignment(existing: MissionTask, candidate: MissionTask) -> bool:
    """Is a task already stored under this id the one this migration would
    write? Compared on what the migration carries over, not on its stamps."""
    return (
        existing.mission_id == candidate.mission_id
        and existing.robot_id == candidate.robot_id
        and existing.schedule == candidate.schedule
        and existing.dispatch_rule == candidate.dispatch_rule
    )


def split_missions(
    missions_dir: str,
    tasks_dir: str,
    out_dir: str,
    clock: Callable[[], str] = now_iso,
) -> SplitReport:
    # "Never modifies the originals" is enforced here, not left to convention:
    # `--out data/missions` would otherwise atomically replace every original
    # with its migrated form, discarding what the new shape has no field for.
    for label, directory in (("out_dir", out_dir), ("tasks_dir", tasks_dir)):
        if _same_dir(directory, missions_dir):
            raise ValueError(
                f"{label} {directory!r} is the same directory as missions_dir {missions_dir!r}; "
                "refusing, because the originals must never be written to"
            )
    report = SplitReport()
    if not os.path.isdir(missions_dir):
        return report
    missions = MissionStore(out_dir)
    tasks = TaskStore(tasks_dir)

    for name in sorted(os.listdir(missions_dir)):
        if not name.endswith(SUFFIX):
            continue
        path = os.path.join(missions_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            mission, task, notes = _migrate_one(raw, name[: -len(SUFFIX)], clock())
            existing = tasks.get_task(task.task_id) if task is not None else None
        except (OSError, ValueError) as exc:  # json errors and ValidationError are ValueErrors
            report.skipped.append(f"{name}: {_brief(exc)}")
            continue

        # Work out which halves are missing rather than assuming a fresh run, so
        # an interrupted migration (task written, mission not) is completed by a
        # rerun instead of needing a file deleted by hand, and a finished one is
        # left alone -- scheduled and unscheduled files alike.
        if existing is not None and not _same_assignment(existing, task):
            report.skipped.append(
                f"{name}: task {task.task_id} already exists and is not this mission's own "
                "assignment; never overwritten"
            )
            continue
        write_task = task is not None and existing is None
        write_mission = not os.path.isfile(os.path.join(out_dir, mission.mission_id + SUFFIX))
        if not write_task and not write_mission:
            report.already_migrated.append(name)
            continue

        # Task first: a crash between the two leaves a task whose mission is
        # missing, which the rerun above completes, not a mission silently
        # stripped of its schedule.
        try:
            if write_task:
                tasks.put_task(task)
                report.tasks += 1
            if write_mission:
                missions.put(mission)
                report.missions += 1
        except OSError as exc:
            report.skipped.append(f"{name}: write failed ({exc}); a rerun completes what is missing")
            continue
        report.notes.extend(notes)
        if task is not None and not (write_task and write_mission):
            report.notes.append(
                f"{mission.mission_id}: completed an interrupted migration; wrote only the "
                f"missing {'task' if write_task else 'mission'}"
            )
    return report


def _brief(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'record'}: {e['msg']}" for e in exc.errors())
    return str(exc)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Split pre-split mission files into missions and tasks.")
    parser.add_argument("--missions", required=True, help="directory of pre-split <id>.json files (read only)")
    parser.add_argument("--out", required=True, help="directory to write migrated missions to")
    parser.add_argument("--tasks", required=True, help="tasks directory to write new tasks into")
    args = parser.parse_args(argv)
    try:
        report = split_missions(args.missions, args.tasks, args.out)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"missions written: {report.missions}")
    print(f"tasks written:    {report.tasks}")
    for label, items in (
        ("already migrated", report.already_migrated), ("skipped", report.skipped), ("notes", report.notes),
    ):
        print(f"{label}: {len(items)}")
        for item in items:
            print(f"  - {item}")
    return 1 if report.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
