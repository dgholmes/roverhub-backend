"""File-backed mission library (2026-10-15).

Missions authored in the console's Missions workspace are saved here so they
survive a browser refresh and are shared between browsers. There is no database
(docs/07-tracker.md), so -- like snapshots (control/snapshots.py) -- this is a
directory of JSON files: one `<mission_id>.json` per mission, each a
`MissionLibraryEntry` (shared/schemas/missions.py) exactly as validated.

The directory is the source of truth and is re-read on every request. A mission
library is small (tens of entries, not thousands of snapshots), so an in-memory
index would buy nothing but a second copy to keep coherent.

What this is not: a mission runtime (mission_executor.py is still empty, so a
saved mission cannot run), a storage layer (no retention, no replication, one
process), or access-controlled (no auth exists anywhere; any caller can
overwrite or delete any mission).

Endpoints:
- GET    /missions[?site_id=]  -> {"items": [entry, ...]}, newest updated_at first
- GET    /missions/{id}        -> entry, 404 if unknown
- PUT    /missions/{id}        -> create or replace; server stamps updated_at and
                                  version; 409 (with the stored entry) on a stale version;
                                  422 for a program whose Stairs steps cannot run (0.0.21,
                                  missions/stairs_checks.py)
- DELETE /missions/{id}        -> 204, 404 if unknown; also deletes the mission's tasks and
                                  their unstarted occurrences (history is kept) and logs it
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.common import now_iso, read_bounded_body
from auth.deps import acting_user_id, require_site_capability, site_visible
from history.audit import AuditFn
from missions import stairs_checks
from shared.schemas.maps import SiteMap
from shared.schemas.missions import MissionLibraryEntry
from tasks.store import TaskStore

logger = logging.getLogger("roverhub.missions")

# Persistent by default, under the repo (gitignored via data/), mirroring
# DEFAULT_SNAPSHOTS_DIR. MISSIONS_DIR overrides it.
DEFAULT_MISSIONS_DIR = str(Path(__file__).resolve().parents[1] / "data" / "missions")

# Concatenated into a path, on a service with no auth: allowlisted, never
# trusted. '.' is allowed for ids like "m-1.2" but a bare "." / ".." is not.
# ':' is deliberately excluded: on NTFS (this backend runs on a Windows dev
# machine) "a:b.json" names an alternate data stream of file "a", not a file.
MISSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# A mission can carry a serialised Blockly workspace (`program_state`), which
# is the bulk of any body. 1 MB is far beyond a realistic one while still
# bounding what an unauthenticated caller can write per file.
MAX_MISSION_BYTES = 1024 * 1024

SUFFIX = ".json"


def _sort_key(entry: MissionLibraryEntry):
    try:
        stamp = datetime.fromisoformat(entry.updated_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        stamp = datetime.min.replace(tzinfo=timezone.utc)
    return (stamp, entry.mission_id)


class MissionStore:
    def __init__(self, missions_dir: str):
        self._dir = missions_dir
        os.makedirs(missions_dir, exist_ok=True)

    def _path(self, mission_id: str) -> str:
        if mission_id in (".", "..") or not MISSION_ID_RE.match(mission_id):
            raise HTTPException(status_code=400, detail="invalid mission_id")
        # Containment check that does not depend on the regex being perfect.
        root = os.path.realpath(self._dir)
        resolved = os.path.realpath(os.path.join(self._dir, mission_id + SUFFIX))
        if os.path.dirname(resolved) != root:
            raise HTTPException(status_code=400, detail="invalid mission_id")
        return resolved

    def _load(self, path: str) -> Optional[MissionLibraryEntry]:
        try:
            with open(path, encoding="utf-8") as handle:
                return MissionLibraryEntry.model_validate_json(handle.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:  # ValidationError is a ValueError
            logger.warning("skipping unreadable mission file %s: %s", path, exc)
            return None

    def list(self, site_id: Optional[str] = None) -> List[MissionLibraryEntry]:
        entries = []
        for name in os.listdir(self._dir):
            if not name.endswith(SUFFIX) or not MISSION_ID_RE.match(name[: -len(SUFFIX)]):
                continue
            entry = self._load(os.path.join(self._dir, name))
            if entry is None:
                continue
            if entry.mission_id != name[: -len(SUFFIX)]:
                logger.warning("skipping mission file %s: mission_id is %r", name, entry.mission_id)
                continue
            if site_id is not None and entry.site_id != site_id:
                continue
            entries.append(entry)
        entries.sort(key=_sort_key, reverse=True)
        return entries

    def get(self, mission_id: str) -> Optional[MissionLibraryEntry]:
        path = self._path(mission_id)
        return self._load(path) if os.path.isfile(path) else None

    def put(self, entry: MissionLibraryEntry) -> None:
        path = self._path(entry.mission_id)
        # Temp file (".<random>.tmp", never listed) in the same directory, then os.replace: a crash leaves
        # either the old mission or the new one, never half of either.
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(entry.model_dump_json())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, mission_id: str) -> bool:
        path = self._path(mission_id)
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False


def build_missions_router(
    missions_dir: str,
    clock: Callable[[], str] = now_iso,
    tasks_dir: Optional[str] = None,
    audit: Optional[AuditFn] = None,
    map_lookup: Optional[Callable[[str], Optional[SiteMap]]] = None,
) -> APIRouter:
    """`clock` returns the UTC ISO string stamped into `updated_at`; injectable
    so ordering is testable. `audit` (history/audit.py AuditRecorder.record)
    is told every create, change and delete.

    `tasks_dir` is where deleting a mission also deletes its tasks. It is
    deliberately not defaulted to the real tasks directory: a router built over a
    temp missions directory (every test) must never reach into `data/tasks`.
    api/app.py passes it; with None, a delete touches the mission only.

    `map_lookup` (map_id -> SiteMap or None, the maps router's store.get) lets a
    save check that a program's Stairs steps name stairs zones on its map. With
    None -- and for a map the store does not have -- only Stairs allowed is
    checked on save; the run's start checks the rest (runs/dispatch.py)."""
    router = APIRouter()
    store = MissionStore(missions_dir)
    task_store = TaskStore(tasks_dir) if tasks_dir is not None else None

    def _audit(action: str, mission_id: str, site_id: Optional[str], detail: dict) -> None:
        if audit is not None:
            audit(action, mission_id, "ok", detail, site_id=site_id)

    def _check_stairs(entry: MissionLibraryEntry) -> None:
        """A program that climbs (0.0.21) must allow stairs, and name stairs
        zones on its map when the map is known. 422 with the sentence."""
        flights = stairs_checks.program_stairs(entry)
        if not flights:
            return
        try:
            stairs_checks.check_allowed(entry, flights)
            site_map = map_lookup(entry.map_id) if map_lookup is not None else None
            if site_map is not None:
                stairs_checks.check_zones(flights, site_map)
        except stairs_checks.StairsNotRunnable as refused:
            raise HTTPException(status_code=422, detail=refused.message) from None

    @router.get("/missions")
    def list_missions(request: Request, site_id: Optional[str] = None):
        # Only the sites the caller may see (auth/deps.py; everyone with sign-in off).
        return {"items": [entry.model_dump(mode="json") for entry in store.list(site_id)
                          if site_visible(request, entry.site_id)]}

    @router.get("/missions/{mission_id}")
    def get_mission(mission_id: str):
        entry = store.get(mission_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="mission not found")
        return entry.model_dump(mode="json")

    # The body is read by hand rather than declared as a parameter so the size
    # cap applies before pydantic parses anything.
    @router.put("/missions/{mission_id}")
    async def put_mission(mission_id: str, request: Request):
        store._path(mission_id)  # validate the id before reading the body
        raw = await read_bounded_body(request, MAX_MISSION_BYTES, "mission")
        try:
            entry = MissionLibraryEntry.model_validate_json(raw)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=json.loads(exc.json(include_url=False))
            ) from None
        if entry.mission_id != mission_id:
            raise HTTPException(status_code=400, detail="body mission_id does not match the path")
        _check_stairs(entry)
        # The guard checked the stored mission's site; this is the body's, so a
        # create or a move lands only where the caller may plan.
        require_site_capability(request, entry.site_id, ("planPatrols",))

        # Compare and write with no await between them: this handler runs on the
        # event loop, so no other request can interleave. The store itself has
        # no locking.
        stored = store.get(mission_id)
        if stored is not None and stored.version != entry.version:
            # The stored entity, so the console can show what changed.
            return JSONResponse(status_code=409, content=stored.model_dump(mode="json"))
        # A create is always version 1, whatever the body carried: it is the first
        # version, and a reader of the file expects that. Only a write to an
        # existing mission increments -- the same ruling as PUT /tasks/{id}. (There
        # is nothing stored to compare a create against, so a stale client
        # re-creating a deleted mission starts over.)
        version = 1 if stored is None else entry.version + 1
        # Who made it is the server's to say: the signed-in user on a create,
        # kept from the stored mission after that (the body's with sign-in off).
        creator = acting_user_id(request, entry.creator) if stored is None else stored.creator
        entry = entry.model_copy(update={"updated_at": clock(), "version": version, "creator": creator})
        store.put(entry)
        _audit("mission.create" if stored is None else "mission.update", mission_id, entry.site_id,
               {"name": entry.name, "kind": entry.kind, "version": version, "map_id": entry.map_id,
                **({"previous_site_id": stored.site_id}
                   if stored is not None and stored.site_id != entry.site_id else {})})
        return entry.model_dump(mode="json")

    @router.delete("/missions/{mission_id}", status_code=204)
    def delete_mission(mission_id: str):
        store._path(mission_id)  # a bad id is a 400 before anything is touched
        # Tasks first, then the mission file: a crash in between leaves a mission
        # with no tasks (harmless) rather than tasks with no mission (which the
        # calendar renders as "Unknown mission"). Runs even when the mission is
        # already gone, so a retry cleans tasks an earlier delete orphaned.
        removed_tasks = []
        if task_store is not None:
            for task in task_store.list_tasks():
                if task.mission_id != mission_id:
                    continue
                result = task_store.delete_task_cascade(task.task_id)
                removed_tasks.append((task.task_id, result))
        if removed_tasks:
            # WARNING, not INFO: nothing configures logging, so INFO is dropped,
            # and a cascade that deletes files should never be invisible.
            logger.warning(
                "deleted mission %s: removed task(s) %s; removed %d unstarted occurrence(s) %s; "
                "kept %d that started or were decided %s",
                mission_id, [t for t, _ in removed_tasks],
                sum(len(r.occurrences_deleted) for _, r in removed_tasks),
                [o for _, r in removed_tasks for o in r.occurrences_deleted],
                sum(len(r.occurrences_kept) for _, r in removed_tasks),
                [o for _, r in removed_tasks for o in r.occurrences_kept],
            )
        existing = store.get(mission_id)
        if not store.delete(mission_id):
            raise HTTPException(status_code=404, detail="mission not found")
        _audit("mission.delete", mission_id, existing.site_id if existing else None,
               {"name": existing.name if existing else None,
                "tasks_deleted": [t for t, _ in removed_tasks]})
        return Response(status_code=204)

    return router
