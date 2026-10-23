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
                                  version; 409 (with the stored entry) on a stale version
- DELETE /missions/{id}        -> 204, 404 if unknown
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
from shared.schemas.missions import MissionLibraryEntry

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


def build_missions_router(missions_dir: str, clock: Callable[[], str] = now_iso) -> APIRouter:
    """`clock` returns the UTC ISO string stamped into `updated_at`; injectable
    so ordering is testable."""
    router = APIRouter()
    store = MissionStore(missions_dir)

    @router.get("/missions")
    def list_missions(site_id: Optional[str] = None):
        return {"items": [entry.model_dump(mode="json") for entry in store.list(site_id)]}

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
        entry = entry.model_copy(update={"updated_at": clock(), "version": version})
        store.put(entry)
        return entry.model_dump(mode="json")

    @router.delete("/missions/{mission_id}", status_code=204)
    def delete_mission(mission_id: str):
        if not store.delete(mission_id):
            raise HTTPException(status_code=404, detail="mission not found")
        return Response(status_code=204)

    return router
