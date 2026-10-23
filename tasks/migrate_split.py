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
from tasks.store import TaskStore

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


def _migrate_one(raw: dict, stem: str, now: str, tasks: TaskStore, report: SplitReport):
    """(mission, task-or-None) for one raw legacy dict. Raises ValueError with a
    reason when it cannot be migrated whole. Touches nothing on disk."""
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
        task = MissionTask.model_validate({
            "task_id": task_id_for(mission_id),
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
        if tasks.get_task(task.task_id) is not None:
            raise ValueError(f"task {task.task_id} already exists and is never overwritten")
        problem = _unusable_schedule(task.schedule)
        if problem:
            notes.append(f"{mission_id}: migrated, but the scheduler cannot use its schedule: {problem}")
    else:
        # No schedule means no task, so these have nowhere to live. Say so.
        if moved["robot_id"]:
            notes.append(f"{mission_id}: robot_id {moved['robot_id']!r} not carried over (no schedule, so no task)")
        if moved["dispatch_rule"] and DispatchRule.model_validate(moved["dispatch_rule"]) != DispatchRule():
            notes.append(f"{mission_id}: non-default dispatch_rule not carried over (no schedule, so no task)")

    report.notes.extend(notes)
    return mission, task


def split_missions(
    missions_dir: str,
    tasks_dir: str,
    out_dir: str,
    clock: Callable[[], str] = now_iso,
) -> SplitReport:
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
            mission, task = _migrate_one(raw, name[: -len(SUFFIX)], clock(), tasks, report)
        except (OSError, ValueError) as exc:  # json errors and ValidationError are ValueErrors
            reason = _brief(exc)
            report.skipped.append(f"{name}: {reason}")
            continue
        # Task first: a crash between the two leaves a task whose mission is
        # missing (visible, and a rerun refuses to overwrite it), not a mission
        # silently stripped of its schedule.
        if task is not None:
            tasks.put_task(task)
            report.tasks += 1
        missions.put(mission)
        report.missions += 1
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
    report = split_missions(args.missions, args.tasks, args.out)
    print(f"missions written: {report.missions}")
    print(f"tasks written:    {report.tasks}")
    for label, items in (("skipped", report.skipped), ("notes", report.notes)):
        print(f"{label}: {len(items)}")
        for item in items:
            print(f"  - {item}")
    return 1 if report.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
