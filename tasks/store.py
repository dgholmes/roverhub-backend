"""File-backed store for mission tasks and their occurrences (2026-10-23).

A `MissionTask` (shared/schemas/tasks.py) assigns a library mission to a robot
on a schedule; a `TaskOccurrence` is one due instance of it. There is no
database (docs/07-tracker.md), so -- exactly like the mission library
(missions/router.py) -- this is a directory of JSON files, each a model
exactly as validated:

    <dir>/tasks/<task_id>.json
    <dir>/occurrences/<occurrence_id>.json

The directories are the source of truth and are re-read on every call. A site
has tens of tasks, and occurrences are materialised only a window ahead, so an
in-memory index would buy nothing but a second copy to keep coherent.

This is the store only. The scheduler that materialises occurrences and the
HTTP router that exposes them are separate. What it is not: a runtime, a
storage layer (no retention, no replication, one process, and no locking --
two writers to one id race, last one wins), or access-controlled (no auth
exists anywhere; any caller can overwrite or delete any record).
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, List, NamedTuple, Optional, Type, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel

from shared.schemas.tasks import MissionTask, TaskOccurrence

logger = logging.getLogger("roverhub.tasks")

# Persistent by default, under the repo (gitignored via data/), mirroring
# DEFAULT_MISSIONS_DIR. TASKS_DIR overrides it.
DEFAULT_TASKS_DIR = str(Path(__file__).resolve().parents[1] / "data" / "tasks")

# Concatenated into a path, on a service with no auth: allowlisted, never
# trusted. '.' is allowed but a bare "." / ".." is not. ':' is deliberately
# excluded: on NTFS (this backend runs on a Windows dev machine) "a:b.json"
# names an alternate data stream of file "a", not a file. Occurrence ids
# ("<task_id>-<compact UTC>", e.g. "t-1-20261024T090000Z") fit the same set,
# which is why one pattern serves both.
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

SUFFIX = ".json"

# The only occurrence statuses that may ever be deleted automatically, and only
# for a task that is gone and only with no `run_id`. All four are states in which
# nothing was ever started: "scheduled" and "due" are promises nobody acted on,
# and "skipped" and "cancelled" are the scheduler's decision *not* to run. With
# the task and mission deleted and no MissionRun pointing at the occurrence there
# is no history left in them -- they render as a run of a mission the operator
# deleted. Everything else is kept: "dispatched"/"running" are in flight (there
# is a window in the scheduler's `_dispatch` between that status write and the
# run existing, and a stuck "dispatched" is a known gap), and "succeeded"/"failed"
# are what happened. A frozen allowlist, not a denylist, so a status added later
# is kept until someone decides otherwise.
PRUNABLE_STATUSES = frozenset({"scheduled", "due", "skipped", "cancelled"})

_M = TypeVar("_M", bound=BaseModel)


def parse_utc(stamp: str) -> Optional[datetime]:
    """UTC-aware datetime from an ISO string, or None if it is not one. A
    string with no offset is read as UTC, the convention for every `due_at`."""
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive means UTC, not local: the spec types `due_at` as "UTC ISO, computed
    # from the schedule's wall clock" (spec 5.2), so a bare timestamp is UTC.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class _JsonDir(Generic[_M]):
    """One directory of `<id>.json` files, each a `model`. Everything the
    mission library does to its single directory, parameterised so tasks and
    occurrences cannot drift apart."""

    def __init__(self, directory: str, model: Type[_M], id_field: str):
        self._dir = directory
        self._model = model
        self._id_field = id_field
        os.makedirs(directory, exist_ok=True)

    def path(self, record_id: str) -> str:
        if record_id in (".", "..") or not TASK_ID_RE.match(record_id):
            raise HTTPException(status_code=400, detail=f"invalid {self._id_field}")
        # Containment check that does not depend on the regex being perfect.
        root = os.path.realpath(self._dir)
        resolved = os.path.realpath(os.path.join(self._dir, record_id + SUFFIX))
        if os.path.dirname(resolved) != root:
            raise HTTPException(status_code=400, detail=f"invalid {self._id_field}")
        return resolved

    def _load(self, path: str) -> Optional[_M]:
        try:
            with open(path, encoding="utf-8") as handle:
                return self._model.model_validate_json(handle.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:  # ValidationError is a ValueError
            logger.warning("skipping unreadable %s file %s: %s", self._model.__name__, path, exc)
            return None

    def all(self) -> List[_M]:
        records = []
        for name in os.listdir(self._dir):
            if not name.endswith(SUFFIX) or not TASK_ID_RE.match(name[: -len(SUFFIX)]):
                continue
            record = self._load(os.path.join(self._dir, name))
            if record is None:
                continue
            record_id = getattr(record, self._id_field)
            if record_id != name[: -len(SUFFIX)]:
                logger.warning("skipping file %s: %s is %r", name, self._id_field, record_id)
                continue
            records.append(record)
        return records

    def get(self, record_id: str) -> Optional[_M]:
        path = self.path(record_id)
        return self._load(path) if os.path.isfile(path) else None

    def put(self, record: _M) -> None:
        path = self.path(getattr(record, self._id_field))
        # Temp file (".<random>.tmp", never listed) in the same directory, then os.replace: a crash leaves
        # either the old record or the new one, never half of either.
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(record.model_dump_json())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, record_id: str) -> bool:
        path = self.path(record_id)
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False


def _unstarted(occ: TaskOccurrence) -> bool:
    """True only for an occurrence that never started anything. `run_id` is
    checked as well as `status`: a run id means a run was created, whatever the
    status says, and that run is history."""
    return occ.status in PRUNABLE_STATUSES and occ.run_id is None


class CascadeResult(NamedTuple):
    """What `delete_task_cascade` did, as occurrence ids, so a caller can log it."""
    task_deleted: bool
    occurrences_deleted: List[str]
    occurrences_kept: List[str]


class TaskStore:
    def __init__(self, tasks_dir: str):
        self._tasks = _JsonDir(os.path.join(tasks_dir, "tasks"), MissionTask, "task_id")
        self._occurrences = _JsonDir(
            os.path.join(tasks_dir, "occurrences"), TaskOccurrence, "occurrence_id"
        )

    # -- tasks ---------------------------------------------------------------

    def list_tasks(self, site_id: Optional[str] = None) -> List[MissionTask]:
        tasks = [t for t in self._tasks.all() if site_id is None or t.site_id == site_id]
        tasks.sort(key=lambda t: t.task_id)
        return tasks

    def get_task(self, task_id: str) -> Optional[MissionTask]:
        return self._tasks.get(task_id)

    def put_task(self, task: MissionTask) -> None:
        self._tasks.put(task)

    def delete_task(self, task_id: str) -> bool:
        return self._tasks.delete(task_id)

    def task_exists(self, task_id: str) -> bool:
        """Whether a task *file* is present -- not whether it parses. `get_task`
        answers None for a corrupt file, and a corrupt file is not a deleted
        task: anything that deletes on "the task is gone" must ask this instead.
        An id the store rejects names no task."""
        try:
            return os.path.isfile(self._tasks.path(task_id))
        except HTTPException:
            return False

    def delete_task_cascade(self, task_id: str) -> CascadeResult:
        """Delete a task and the occurrences that never started. The task file
        goes first, so a crash part-way leaves orphans (which `prune_orphan_occurrences`
        finishes) rather than a live task with half its queue. Occurrences that
        started or were decided stay, by `PRUNABLE_STATUSES`. Idempotent: for a
        task that is already gone it still clears whatever unstarted occurrences
        it left behind."""
        task_deleted = self._tasks.delete(task_id)
        deleted: List[str] = []
        kept: List[str] = []
        for occ in self.occurrences_for_task(task_id):
            if _unstarted(occ):
                # A concurrent delete is not a failure of this one.
                self._occurrences.delete(occ.occurrence_id)
                deleted.append(occ.occurrence_id)
            else:
                kept.append(occ.occurrence_id)
        return CascadeResult(task_deleted, deleted, kept)

    # -- occurrences ---------------------------------------------------------

    def list_occurrences(
        self,
        site_id: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[TaskOccurrence]:
        """Occurrences with `start <= due_at < end`, oldest first, ties broken by
        occurrence_id. An occurrence whose `due_at` does not parse sorts before
        every real one. Either bound may be None. Half-open so adjacent windows
        never share a slot. `due_at` is compared as an instant, not as text, so
        an offset-bearing bound still means what it says. An unparseable `due_at`
        cannot be placed in a window, so a bounded listing leaves it out rather
        than guessing; an unbounded one still returns it."""
        lo = parse_utc(start) if start is not None else None
        hi = parse_utc(end) if end is not None else None
        if (start is not None and lo is None) or (end is not None and hi is None):
            raise HTTPException(status_code=400, detail="invalid start or end")
        bounded = lo is not None or hi is not None
        floor = datetime.min.replace(tzinfo=timezone.utc)
        keyed = []
        for occ in self._occurrences.all():
            if site_id is not None and occ.site_id != site_id:
                continue
            due = parse_utc(occ.due_at)
            if bounded:
                if due is None or (lo is not None and due < lo) or (hi is not None and due >= hi):
                    continue
            keyed.append((due or floor, occ.occurrence_id, occ))
        keyed.sort(key=lambda item: item[:2])
        return [occ for _, _, occ in keyed]

    def get_occurrence(self, occurrence_id: str) -> Optional[TaskOccurrence]:
        return self._occurrences.get(occurrence_id)

    def put_occurrence(self, occ: TaskOccurrence) -> None:
        self._occurrences.put(occ)

    def delete_occurrence(self, occurrence_id: str) -> bool:
        return self._occurrences.delete(occurrence_id)

    def occurrences_for_task(self, task_id: str) -> List[TaskOccurrence]:
        """Every readable occurrence of one task, oldest first. Exact match on
        `task_id`, never a prefix: "t-1" must not return "t-10"'s."""
        return [o for o in self.list_occurrences() if o.task_id == task_id]

    def prune_orphan_occurrences(self) -> List[str]:
        """Delete occurrences whose task no longer exists, and return their ids.

        Narrower than "has no task": an occurrence goes only if it never started
        anything (`PRUNABLE_STATUSES`, no `run_id`). `due_at` is irrelevant -- a
        past-dated "skipped" row of a deleted task is the same debris as a future
        "scheduled" one, and keeping it would bring the bug back a day later.
        "No task" means no task *file* (`task_exists`): a corrupt one is not a
        deleted one.
        """
        pruned: List[str] = []
        for occ in self.list_occurrences():
            if not _unstarted(occ):
                continue
            try:
                if os.path.isfile(self._tasks.path(occ.task_id)):
                    continue
            except HTTPException:  # a task_id the store rejects: not ours to judge
                continue
            self._occurrences.delete(occ.occurrence_id)
            pruned.append(occ.occurrence_id)
        return pruned
