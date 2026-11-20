"""Map endpoints: upload a packed survey, list and read maps, serve their files.

- GET    /maps[?site_id=]                       -> {"items": [SiteMap, ...]}
- GET    /maps/{map_id}                         -> SiteMap, 404 if unknown
- POST   /maps                                  -> 201 + SiteMap; replaces any map with that id
- GET    /maps/{map_id}/floors/{index}/{kind}   -> the floor's `grid` (PGM), `preview`
                                                   (PNG), `cloud` (float32 xyz), `track`
                                                   (csv), `survey` (json), `moved` (the
                                                   "probably moved" layer, json) or
                                                   `moved_cloud` (its points, PCD); 404 if
                                                   there is no such file, 413 if a cloud is
                                                   over 8 MiB
- GET    /maps/{map_id}/floors/{index}/planning -> the floor's planning grid (PGM), the map
                                                   Nav2 plans on; 404 if it has none
- PUT    /maps/{map_id}/floors/{index}/planning -> SiteMap; the raw PGM as the body
         [?clearance_m=0.2]                        sets it (floor.planning gets its counts)
                                                   AS A NEW VERSION: the map drops to draft
                                                   (zones kept) and a quality check is
                                                   queued (headers X-Map-Version,
                                                   X-Quality-Job); 400 if it is not a P5 PGM
                                                   of exactly the floor's size_cells, 404
                                                   for an unknown map or floor, 413 over
                                                   32 MiB
- DELETE /maps/{map_id}/floors/{index}/planning -> SiteMap without it; 404 if there is none.
                                                   In place, status kept (no version)
- PUT    /maps/{map_id}/floors/{index}/moved    -> {"map": SiteMap, "version", "quality_job_id",
                                                   "blobs", "dropped", "added",
                                                   "moved_cloud_points_removed"}; the body is
                                                   the whole moved.json. "Keep" is this with
                                                   the kept blob left out: the blob's points
                                                   leave moved.pcd, the cloud and grids are
                                                   untouched. A new version, draft, quality
                                                   queued, like PUT planning. 400 for a layer
                                                   that does not parse (maps/cloud_edit.py
                                                   parse_moved), 404, 413 over 4 MiB.
         [?frame=map|survey]                       Or multipart: a `moved` part (moved.json)
                                                   and an optional `moved_cloud` file part
                                                   (moved.pcd), which then REPLACES the
                                                   floor's moved.pcd ("moved_cloud_replaced":
                                                   true) -- a re-run of the moving-object
                                                   search (FIND_MOVING). frame=survey: both
                                                   are in the frame of the cloud sent to POST
                                                   /map-surveys and are carried through the
                                                   levelling it got (its survey.json); 400
                                                   for a floor that has none
- POST   /maps/{map_id}/floors/{index}/cloud-edits
                                                -> 202 + job (kind "cloud_edit"); body
                                                   {"delete_boxes": [{"min": [x,y,z], "max":
                                                   [x,y,z]}], "resolve_moved": ["b001"],
                                                   "keep_moved": ["b002"], "reason": "..."},
                                                   every key optional but one. The job
                                                   deletes the points, re-rasterises the
                                                   planning grid under them
                                                   (maps/cloud_edit.py), saves ONE new
                                                   version (draft) and queues the quality
                                                   check (result.quality_job_id). 400 for a
                                                   bad box (min > max, not finite, over
                                                   500), an unknown blob, a floor with no
                                                   cloud, or resolve_moved on a floor with no
                                                   moved.pcd (its points are what is matched);
                                                   404; 413 over 1 MiB
- PUT    /maps/{map_id}/zones                   -> SiteMap; body {"zones": [MapZone, ...]}
                                                   replaces the map's zones; 404 if unknown,
                                                   400 if a zone cannot be stored
- PUT    /maps/{map_id}/status                  -> SiteMap; body {"status": "draft" | "active"
                                                   | "archived"}; 400 to activate a map with no
                                                   floors
- DELETE /maps/{map_id}                         -> 204, 404 if unknown

The survey pipeline, quality and versions (tracker 1-5-02, -04, -05, -06):

- POST   /map-surveys                           -> 202 + job; multipart: file parts `cloud`
                                                   (.pcd, raw or levelled) and `track` (t,x,y,z
                                                   csv), text fields `site_id`, `name`, and
                                                   optional `map_id` (default "map-<slug of
                                                   name>"), `floor` (0), `resolution_m` (0.05),
                                                   `track_frame` (auto | raw | level),
                                                   `survey_id` (1-64 chars: the console survey
                                                   that made it) and `robot_id` (a robot at
                                                   `site_id`; a device key's own robot always
                                                   wins), and `floor_label` (at most 40 chars,
                                                   e.g. "2F": the floor's label; not sent, a
                                                   re-surveyed floor keeps its own).
                                                   `survey_id` and `robot_id` go into the
                                                   job's detail and
                                                   the floor's survey.json ("survey": {...}),
                                                   null when not sent. The job
                                                   levels it, grids it, stores it as a draft
                                                   (maps/survey.py), then runs the quality
                                                   report. 413 over max_survey_bytes
- GET    /map-jobs[?map_id=]                    -> {"items": [job, ...]}, newest first
- GET    /map-jobs/{job_id}                     -> job: {job_id, kind, map_id, site_id, status
                                                   (queued|running|succeeded|failed), stage,
                                                   created_at, started_at, finished_at, error,
                                                   result}; 404 if unknown
- GET    /maps/{map_id}/quality                 -> the stored report plus "current": whether
                                                   it was computed from the files there now;
                                                   404 if none has run
- POST   /maps/{map_id}/quality                 -> 202 + job (re)computing it; a queued one
                                                   for the same map is joined, not repeated
- GET    /maps/{map_id}/versions                -> {"items": [...]}: the live bundle first
                                                   ("current": true), then the kept ones
- GET    /maps/{map_id}/versions/{v}/floors/{index}/{kind}
                                                -> an archived version's file, as above
- POST   /maps/{map_id}/versions/{v}/restore    -> SiteMap; version v becomes the live bundle
                                                   again (as a new version), zones kept, draft
- POST   /maps/{map_id}/floors/{index}/trim     -> SiteMap; the floor cut to its walked track
                                                   + trim.TRIM_MARGIN_M as a new version, draft;
                                                   unchanged (no version) when already inside;
                                                   409 for a zone it would cut or no track
- PATCH  /maps/{map_id}                         -> SiteMap; {"name"?, "floors"?: [{"index",
                                                   "label"}]}: names only, outside the content
                                                   hash, so no new version and no drop to draft

A quality job is also queued after every upload, restore, planning-grid
change and map-editor save, so the console usually finds a fresh report waiting. PUT status
`active` refuses with 409 unless the report is current (detail.code
`quality_missing` / `quality_stale`) and passed (`quality_failed`, with
`failed`); `{"status": "active", "override_quality": true, "reason": "..."}`
freezes a failed one for a caller holding `override` at the map's site, and
the audit log records who, why, and which checks failed. Before that, freezing
a map with no zones is a 409 `zones_unreviewed` unless the body says
`"zones_reviewed": true` (none are needed); the map then records when and by
whom -- the signed-in user, never the body (0.0.29).

POST takes the bundle tools/map_prep/pack_map.py writes as multipart form data,
one file part per bundle file, **the part's field name being the file's path in
the bundle**:

    curl -F site_map.json=@site_map.json -F floor0/grid.pgm=@floor0/grid.pgm \\
         -F floor0/grid.png=@floor0/grid.png -F floor0/cloud.bin=@floor0/cloud.bin \\
         http://localhost:8000/maps

A floor's `floor<N>/planning.pgm` may be sent as well; it is optional. So may
`floor<N>/track.csv`, the path the survey walked in the bundle's (levelled)
frame: the quality report needs it for path coverage, drift and track tilt. So
may `floor<N>/moved.json` and `floor<N>/moved.pcd`, the "probably moved" layer
tools/map_prep/transient_filter.py writes (pack_map.py --moved/--moved-cloud).

The field name, not the filename, because a filename is whatever a browser or
client chooses to send, while a field name is carried verbatim. Only the paths
the bundle layout names are accepted (`PART_NAME_RE`); anything else is a 400,
so a part can never choose where it is written. The parts are written into a
scratch directory and handed to `MapStore.save_bundle`, which validates and
stores them.

Access is the rules in auth/policy.py: reading is open to every role, uploading
or deleting a map is site configuration, and drawing zones and freezing a map
(`editZones`) is the supervisor's review of it. Setting or removing a floor's
planning grid is the same review as drawing zones -- both say where the robot
may walk. The map editor's saves (PUT planning, PUT moved, POST cloud-edits)
drop the map to draft, so the supervisor's sign-off is still needed after
them: the engineer who uploads a survey may clean it too, so they are
`editZones` or `configure`. Removing a planning grid is `editZones`.
A survey upload and a version restore replace the bundle, like an upload, so
they are `configure`; running the quality check is either role's; freezing a
map whose report failed also needs `override`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
import logging
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from api.common import read_bounded_body
from auth.deps import principal_of, require_site_capability, site_visible
from history.audit import AuditFn
from shared.schemas.maps import MapStatus, MapZone, SiteMap

from .jobs import JobRunner
from .quality import is_current, run_quality
from .store import (
    ARTIFACT_FILES, DEFAULT_CLEARANCE_M, DEFAULT_MAPS_DIR, MAX_CLOUD_BYTES, MAX_MOVED_BYTES, MAX_PLANNING_BYTES,
    MAX_TRACK_BYTES, MOVED_FRAMES, UPLOAD_PREFIX, BundleError, FloorNotFound, MapNotFound, MapStore,
    PlanningNotFound, VersionNotFound, valid_map_id,
)

logger = logging.getLogger("roverhub.maps")

# Per-part limits. A cloud's is the packer's own figure (see store.MAX_CLOUD_BYTES).
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
# The request as a whole, counted as it arrives (see `upload_map`): one floor's
# worth of everything at its limit, with room for a second floor's grid and
# preview. The per-part limits above apply to what is kept; this one bounds what
# is received, which the multipart parser would otherwise spool to disk unchecked.
MAX_UPLOAD_BYTES = 128 * 1024 * 1024

# The zone list as the console sends it. A zone is a label and a few dozen
# points, so this is room for hundreds of them.
MAX_ZONES_BODY_BYTES = 1024 * 1024

PART_NAME_RE = re.compile(
    r"site_map\.json|floor[0-9]{1,3}/(?:grid\.pgm|grid\.png|cloud\.bin|planning\.pgm|track\.csv"
    r"|moved\.json|moved\.pcd)")
MEDIA_TYPES = {"grid": "image/x-portable-graymap", "preview": "image/png",
               "cloud": "application/octet-stream", "planning": "image/x-portable-graymap",
               "track": "text/csv", "survey": "application/json",
               "moved": "application/json", "moved_cloud": "application/octet-stream"}

# A survey upload: the raw cloud is the bulk. The 2026-11-09 office survey's
# .pcd is 88 MB for 2.76 M points with normals; 1 GiB is a building ten times
# that, and survey_math.MAX_POINTS refuses more points than the server holds.
MAX_SURVEY_BYTES = 1024 * 1024 * 1024
SURVEY_FIELDS = ("site_id", "name", "map_id", "floor", "resolution_m", "track_frame", "survey_id", "robot_id",
                 "floor_label")
# File parts of a survey upload. `moved` / `moved_cloud` (optional) are the
# "probably moved" layer, in the uploaded cloud's frame (maps/survey.py).
SURVEY_FILES = ("cloud", "track", "moved", "moved_cloud")
_CHUNK = 1024 * 1024


class _TooLarge(Exception):
    pass


class _StatusBody(BaseModel):
    status: MapStatus
    # Freeze although the quality report failed. Needs `override` at the
    # map's site; `reason` goes to the audit log.
    override_quality: bool = False
    reason: Optional[str] = Field(default=None, max_length=500)
    # 0.0.29: the person freezing a map with no zones says none are needed
    # (the freeze dialog's "Activate anyway"). Without it that is a 409
    # `zones_unreviewed`. Who said so is the signed-in user, never the body.
    zones_reviewed: bool = False


class _FloorLabel(BaseModel):
    index: int
    label: Optional[str] = None


class _RenameBody(BaseModel):
    """PATCH /maps/{id}: either or both. Bounds are the store's (MapStore.rename)."""
    name: Optional[str] = None
    floors: Optional[List[_FloorLabel]] = Field(default=None, max_length=1000)


def _limit_for(name: str) -> int:
    if name == "site_map.json":
        return MAX_MANIFEST_BYTES
    if name.endswith("cloud.bin"):
        return MAX_CLOUD_BYTES
    if name.endswith("track.csv"):
        return MAX_TRACK_BYTES
    if name.endswith("moved.json"):
        return MAX_MOVED_BYTES
    if name.endswith("moved.pcd"):
        return MAX_CLOUD_BYTES
    return MAX_IMAGE_BYTES


def build_maps_router(maps_dir: str = DEFAULT_MAPS_DIR,
                      max_upload_bytes: int = MAX_UPLOAD_BYTES,
                      on_status: Optional[Callable[[SiteMap], None]] = None,
                      audit: Optional[AuditFn] = None,
                      jobs_dir: Optional[str] = None,
                      start_jobs: bool = True,
                      max_survey_bytes: int = MAX_SURVEY_BYTES,
                      robot_site_of: Optional[Callable[[str], Optional[str]]] = None) -> APIRouter:
    """`on_status(map)` after a status change is stored (the audit log).
    `audit` (history/audit.py AuditRecorder.record) is told every upload,
    zone edit, planning-grid change, delete, restore and quality override.

    Background jobs keep their records in `jobs_dir`, by default `map-jobs`
    beside `maps_dir` (outside the store, whose directory names are map ids).
    `start_jobs=False` (tests) queues jobs without a worker thread; call
    `router.jobs.run_pending()`.

    `robot_site_of(robot_id)` -> the site a robot registered at, or None: a
    survey upload's `robot_id` field must name a robot at the survey's site.
    Without it (unit tests) the field is taken as sent."""
    router = APIRouter()
    store = MapStore(maps_dir)
    router.store = store  # type: ignore[attr-defined]  # tests and the app share it
    jobs = JobRunner(jobs_dir or (Path(os.path.abspath(maps_dir)).parent / "map-jobs"), start=start_jobs)
    router.jobs = jobs  # type: ignore[attr-defined]

    def _requested_by(request: Request) -> Optional[str]:
        principal = principal_of(request)
        return principal.id if principal is not None else None

    def _queue_quality(map_id: str, site_id: Optional[str], requested_by: Optional[str]) -> dict:
        waiting = jobs.active("quality", map_id)
        if waiting is not None:
            return waiting
        return jobs.submit("quality", map_id, site_id,
                           lambda progress: run_quality(store, map_id, progress),
                           requested_by=requested_by)

    def _queue_quality_quietly(site_map: Optional[SiteMap], request: Request) -> Optional[str]:
        """After a change that makes the report stale. Never fails the change."""
        if site_map is None:
            return None
        try:
            return _queue_quality(site_map.map_id, site_map.site_id, _requested_by(request))["job_id"]
        except Exception:
            logger.exception("could not queue a quality check of map %s", site_map.map_id)
            return None

    def _device_of(request: Request) -> Optional[str]:
        """The robot whose device key made this request, or None for a person.
        A robot uploads the draft of a survey it made (auth/policy.py
        `device_own_site`); the audit row then names it as the robot too."""
        principal = principal_of(request)
        return principal.id if principal is not None and principal.kind == "device" else None

    def _audit(action: str, site_map: Optional[SiteMap], map_id: str, detail: dict,
               site_id: Optional[str] = None, actor: Optional[str] = None,
               request: Optional[Request] = None) -> None:
        if audit is None:
            return
        # `actor` only off a request (a job's worker thread has none bound).
        extra: dict = {"actor": actor} if actor is not None else {}
        device = _device_of(request) if request is not None else None
        if device is not None:
            # The actor is already the device (the request's principal); say so
            # in the row as well, so "rover-jetson" is not read as a person.
            detail = {**detail, "by_device": device}
            extra["robot_id"] = device
        try:
            audit(action, map_id, "ok", detail,
                  site_id=site_id or (site_map.site_id if site_map is not None else None), **extra)
        except Exception:
            logger.exception("could not audit %s of map %s", action, map_id)

    def _manifest_site(part: UploadFile) -> tuple:
        """(map_id, site_id) the bundle's manifest names, read before anything
        is stored so the upload can be checked against the caller's sites. A
        manifest that does not parse is left for the store to refuse."""
        try:
            raw = part.file.read(MAX_MANIFEST_BYTES + 1)
            part.file.seek(0)
            data = json.loads(raw)
        except (OSError, ValueError):
            return None, None
        # The bundle's map fields sit in its `site_map` block (store._read_bundle).
        block = data.get("site_map") if isinstance(data, dict) else None
        if not isinstance(block, dict):
            return None, None
        map_id, site_id = block.get("map_id"), block.get("site_id")
        return (map_id if isinstance(map_id, str) else None), (site_id if isinstance(site_id, str) else None)

    def _ingest(parts: Dict[str, UploadFile]) -> SiteMap:
        with tempfile.TemporaryDirectory(dir=store.root, prefix=UPLOAD_PREFIX) as scratch:
            for name, part in parts.items():
                target = Path(scratch) / name  # `name` passed PART_NAME_RE: no ".." or absolute
                target.parent.mkdir(parents=True, exist_ok=True)
                limit = _limit_for(name)
                size = 0
                with open(target, "wb") as handle:
                    while True:
                        chunk = part.file.read(_CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            raise _TooLarge("%s exceeds %d bytes" % (name, limit))
                        handle.write(chunk)
            return store.save_bundle(Path(scratch))

    def _curate(write):
        try:
            return write()
        except MapNotFound:
            raise HTTPException(status_code=404, detail="map not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @router.get("/maps")
    def list_maps(request: Request, site_id: Optional[str] = None):
        # Only the sites the caller may see (auth/deps.py; everyone with sign-in off).
        return {"items": [m.model_dump(mode="json") for m in store.list()
                          if (site_id is None or m.site_id == site_id) and site_visible(request, m.site_id)]}

    @router.get("/maps/{map_id}")
    def get_map(map_id: str):
        site_map = store.get(map_id)
        if site_map is None:
            raise HTTPException(status_code=404, detail="map not found")
        return site_map

    @router.post("/maps", status_code=201)
    async def upload_map(request: Request):
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_upload_bytes:
            raise HTTPException(status_code=413, detail="upload exceeds %d bytes" % max_upload_bytes)
        # The declared length only covers a client that declares one. A chunked
        # body has none, so also count the bytes as they are read and stop at the
        # cap, before the multipart parser can spool the rest to disk. The same two
        # layers as api/common.read_bounded_body, for a body that is parsed rather
        # than read whole.
        received = 0

        async def counting_receive():
            nonlocal received
            message = await request.receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_upload_bytes:
                    raise HTTPException(status_code=413, detail="upload exceeds %d bytes" % max_upload_bytes)
            return message

        form = await Request(request.scope, counting_receive).form()
        try:
            parts: Dict[str, UploadFile] = {}
            for name, part in form.multi_items():
                if not isinstance(part, UploadFile):
                    raise HTTPException(status_code=400, detail="%r is not a file part" % name)
                if not PART_NAME_RE.fullmatch(name):
                    raise HTTPException(status_code=400, detail="%r is not a file the bundle layout names" % name)
                if name in parts:
                    raise HTTPException(status_code=400, detail="%r was sent twice" % name)
                parts[name] = part
            if not parts:
                raise HTTPException(status_code=400, detail="no bundle files were sent")
            # The guard could not see a site (it is in the body): configure is
            # needed at the bundle's site, and at the site of a map it replaces.
            existing = None
            if "site_map.json" in parts:
                new_id, new_site = await run_in_threadpool(_manifest_site, parts["site_map.json"])
                require_site_capability(request, new_site, ("configure",))
                if new_id is not None:
                    existing = store.get(new_id)
                    if existing is not None and existing.site_id != new_site:
                        require_site_capability(request, existing.site_id, ("configure",))
            try:
                saved = await run_in_threadpool(_ingest, parts)
                _audit("map.upload", saved, saved.map_id,
                       {"name": saved.name, "floors": len(saved.floors), "status": saved.status,
                        "replaced": existing is not None})
                job_id = _queue_quality_quietly(saved, request)
                headers = {"X-Quality-Job": job_id} if job_id else {}
                return Response(saved.model_dump_json(), status_code=201, media_type="application/json",
                                headers=headers)
            except _TooLarge as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from None
            except BundleError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        finally:
            await form.close()

    def _serve(map_id: str, index: int, kind: str) -> FileResponse:
        path = store.artifact_path(map_id, index, kind)
        if path is None:
            raise HTTPException(status_code=404, detail="no such file")
        if kind in ("cloud", "moved_cloud") and os.path.getsize(path) > MAX_CLOUD_BYTES:
            raise HTTPException(status_code=413, detail="cloud exceeds %d bytes" % MAX_CLOUD_BYTES)
        # A re-upload replaces the files under the same URL, so make the browser
        # revalidate (FileResponse supplies the ETag) rather than guess.
        return FileResponse(path, media_type=MEDIA_TYPES[kind], headers={"Cache-Control": "no-cache"})

    def _planning(write):
        try:
            return write()
        except (FloorNotFound, PlanningNotFound) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except MapNotFound:
            raise HTTPException(status_code=404, detail="map not found") from None
        except ValueError as exc:  # BundleError: not a PGM, the wrong size, a bad clearance
            raise HTTPException(status_code=400, detail=str(exc)) from None

    # The planning routes are declared before the generic artifact route so
    # that each method of this path has a rule of its own in auth/policy.py.
    @router.get("/maps/{map_id}/floors/{index}/planning")
    def get_planning(map_id: str, index: int):
        return _serve(map_id, index, "planning")

    def _edited(saved: SiteMap, version: int, request: Request) -> Optional[str]:
        """After a map-editor save: queue the quality check, answer with headers."""
        return _queue_quality_quietly(saved, request)

    # The body is the PGM itself, read by hand so the size cap applies before
    # anything is parsed (as for zones). The map editor's brush: a new version,
    # draft, quality queued (MapStore.edit_planning).
    @router.put("/maps/{map_id}/floors/{index}/planning")
    async def put_planning(map_id: str, index: int, request: Request,
                           clearance_m: float = DEFAULT_CLEARANCE_M):
        raw = await read_bounded_body(request, MAX_PLANNING_BYTES, "planning grid")
        saved, version = await run_in_threadpool(
            _planning, lambda: store.edit_planning(map_id, index, raw, clearance_m))
        floor = next(f for f in saved.floors if f.index == index)
        _audit("map.planning.set", saved, map_id,
               {"floor": index, "clearance_m": clearance_m, "bytes": len(raw), "version": version,
                "free_cells": floor.planning.free_cells if floor.planning else None,
                "occupied_cells": floor.planning.occupied_cells if floor.planning else None},
               request=request)
        job_id = _edited(saved, version, request)
        headers = {"X-Map-Version": str(version)}
        if job_id:
            headers["X-Quality-Job"] = job_id
        return Response(saved.model_dump_json(), media_type="application/json", headers=headers)

    @router.delete("/maps/{map_id}/floors/{index}/planning")
    def delete_planning(map_id: str, index: int, request: Request):
        saved = _planning(lambda: store.delete_planning(map_id, index))
        _audit("map.planning.delete", saved, map_id, {"floor": index})
        _queue_quality_quietly(saved, request)
        return saved

    # --- the map editor: the moved layer and cloud edits (maps/cloud_edit.py) -

    async def _moved_parts(request: Request) -> tuple:
        """(moved.json bytes, moved.pcd bytes or None) from a multipart PUT
        moved: a `moved` part (file or text) and an optional `moved_cloud`
        file part. Counted as it arrives, like a survey upload."""
        limit = MAX_MOVED_BYTES + MAX_CLOUD_BYTES + 64 * 1024
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise HTTPException(status_code=413, detail="the moved layer upload exceeds %d bytes" % limit)
        received = 0

        async def counting_receive():
            nonlocal received
            message = await request.receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="the moved layer upload exceeds %d bytes" % limit)
            return message

        form = await Request(request.scope, counting_receive).form(max_files=2, max_fields=1)
        try:
            found: Dict[str, bytes] = {}
            for name, part in form.multi_items():
                if name not in ("moved", "moved_cloud"):
                    raise HTTPException(status_code=400, detail="%r is not part of a moved layer upload" % name)
                if name in found:
                    raise HTTPException(status_code=400, detail="%r was sent twice" % name)
                cap = MAX_MOVED_BYTES if name == "moved" else MAX_CLOUD_BYTES
                if isinstance(part, UploadFile):
                    data = await run_in_threadpool(part.file.read, cap + 1)
                elif name == "moved":
                    data = part.encode("utf-8")
                else:
                    raise HTTPException(status_code=400, detail="'moved_cloud' must be a file part")
                if len(data) > cap:
                    raise HTTPException(status_code=413, detail="the %s exceeds %d bytes" % (
                        "moved layer" if name == "moved" else "moved cloud", cap))
                found[name] = data
            if "moved" not in found:
                raise HTTPException(status_code=400, detail="the upload needs a 'moved' part (moved.json)")
            return found["moved"], found.get("moved_cloud")
        finally:
            await form.close()

    @router.put("/maps/{map_id}/floors/{index}/moved")
    async def put_moved(map_id: str, index: int, request: Request, frame: str = "map"):
        # frame=survey: the layer is in the frame of the cloud that was sent to
        # POST /map-surveys (the robot's re-run of the moving-object search,
        # FIND_MOVING); it is carried through the levelling that survey got.
        if frame not in MOVED_FRAMES:
            raise HTTPException(status_code=400, detail="frame must be one of: %s" % ", ".join(MOVED_FRAMES))
        moved_cloud = None
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raw, moved_cloud = await _moved_parts(request)
        else:
            raw = await read_bounded_body(request, MAX_MOVED_BYTES, "moved layer")
        saved, version, outcome = await run_in_threadpool(
            _planning, lambda: store.set_moved(map_id, index, raw, frame=frame, moved_cloud=moved_cloud))
        _audit("map.moved.set", saved, map_id,
               {"floor": index, "version": version, "blobs": outcome["blobs"],
                "dropped": len(outcome["dropped"]), "dropped_ids": outcome["dropped"][:50],
                "added": len(outcome["added"]),
                "moved_cloud_points_removed": outcome["moved_cloud_points_removed"],
                "moved_cloud_replaced": outcome["moved_cloud_replaced"], "frame": frame},
               request=request)
        job_id = _edited(saved, version, request)
        return {"map": saved.model_dump(mode="json"), "version": version, "quality_job_id": job_id, **outcome}

    @router.post("/maps/{map_id}/floors/{index}/cloud-edits", status_code=202)
    async def post_cloud_edits(map_id: str, index: int, request: Request):
        from .cloud_edit import (
            MAX_CLOUD_EDIT_BODY_BYTES, check_against_floor, parse_cloud_edit, parse_moved, run_cloud_edit,
        )

        raw = await read_bounded_body(request, MAX_CLOUD_EDIT_BODY_BYTES, "cloud edit")
        try:
            edit = parse_cloud_edit(raw)
        except BundleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        site_map = store.get(map_id)
        if site_map is None:
            raise HTTPException(status_code=404, detail="map not found")
        if not any(f.index == index for f in site_map.floors):
            raise HTTPException(status_code=404, detail="map %s has no floor %d" % (map_id, index))

        def check() -> None:
            # Refused now, with the reason, rather than as a failed job; the
            # job checks again against what it reads.
            moved_path = store.artifact_path(map_id, index, "moved")
            moved = parse_moved(moved_path.read_bytes()) if moved_path is not None else None
            check_against_floor(edit, store.artifact_path(map_id, index, "cloud") is not None, moved,
                                store.artifact_path(map_id, index, "moved_cloud") is not None)

        try:
            await run_in_threadpool(check)
        except BundleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        requested_by = _requested_by(request)
        job_id = jobs.new_id()

        def work(progress):
            result = run_cloud_edit(store, map_id, index, edit, progress, detail={"job_id": job_id})
            saved = store.get(map_id)
            result["quality_job_id"] = None
            try:
                result["quality_job_id"] = _queue_quality(map_id, saved.site_id if saved else None,
                                                          requested_by)["job_id"]
            except Exception:
                logger.exception("could not queue a quality check of map %s", map_id)
            planning = result["planning"]
            _audit("map.cloud_edit", saved, map_id,
                   {"job_id": job_id, "floor": index, "version": result["version"],
                    "boxes": len(edit.delete_boxes), "resolved_moved": len(edit.resolve_moved),
                    "kept_moved": len(edit.keep_moved), "points_removed": result["points_removed"],
                    "moved_cloud_points_removed": result["moved_cloud_points_removed"],
                    "cells_freed": planning.get("freed_cells", 0),
                    "grid_cells_freed": result["localisation"]["freed_cells"], "reason": edit.reason},
                   site_id=site_map.site_id, actor=requested_by)
            return result

        return jobs.submit("cloud_edit", map_id, site_map.site_id, work, requested_by=requested_by,
                           job_id=job_id,
                           detail={"floor": index, "boxes": len(edit.delete_boxes),
                                   "resolve_moved": edit.resolve_moved[:50], "keep_moved": edit.keep_moved[:50],
                                   "reason": edit.reason})

    @router.get("/maps/{map_id}/floors/{index}/{kind}")
    def get_artifact(map_id: str, index: int, kind: str):
        return _serve(map_id, index, kind)

    # The zone list is read by hand, as the missions router does, so the size cap
    # applies before pydantic parses anything.
    @router.put("/maps/{map_id}/zones")
    async def put_zones(map_id: str, request: Request):
        raw = await read_bounded_body(request, MAX_ZONES_BODY_BYTES, "zones")
        try:
            zones = [MapZone.model_validate(z) for z in json.loads(raw)["zones"]]
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False))) from None
        except (ValueError, KeyError, TypeError):
            raise HTTPException(status_code=422, detail='body must be {"zones": [...]}') from None
        saved = _curate(lambda: store.save_zones(map_id, zones))
        _audit("map.zones", saved, map_id,
               {"zones": len(zones), "kinds": sorted({z.kind for z in zones})})
        return saved

    def _zones_gate(map_id: str, body: _StatusBody) -> None:
        """Freezing (draft -> active) a map with no zones needs the person to
        say none are needed: the lidar cannot see stairs, drops or glass at
        floor level, and a zone is the only thing that keeps a robot off them
        (spec §4.8). Asked before the quality gate, so an override is never
        audited for a freeze this refuses."""
        current = store.get(map_id)
        if current is None:
            raise HTTPException(status_code=404, detail="map not found")
        if body.status != "active" or current.status == "active" or not current.floors:
            return
        if not current.zones and not body.zones_reviewed:
            raise HTTPException(status_code=409, detail={
                "code": "zones_unreviewed",
                "message": "no zones are drawn on this map. The lidar cannot see stairs, drops or glass at "
                           "floor level; draw zones over them, or confirm none are needed (zones_reviewed)"})

    def _quality_gate(map_id: str, body: _StatusBody, request: Request) -> None:
        """Freezing (draft -> active) needs a current, passed quality report,
        or `override_quality` from a caller holding `override`. A map with no
        floors is left to set_status, which refuses it with its own reason;
        re-sending `active` for an active map changes nothing and is let be."""
        current = store.get(map_id)
        if current is None:
            raise HTTPException(status_code=404, detail="map not found")
        if body.status != "active" or current.status == "active" or not current.floors:
            return
        report = store.read_quality(map_id)
        if report is None:
            raise HTTPException(status_code=409, detail={
                "code": "quality_missing",
                "message": "this map has no quality report; run one (POST /maps/%s/quality) first" % map_id})
        if not is_current(store, map_id, report):
            raise HTTPException(status_code=409, detail={
                "code": "quality_stale",
                "message": "the quality report was made from files this map no longer holds; run it again"})
        if report.get("passed"):
            return
        failed = report.get("failed", [])
        if not body.override_quality:
            raise HTTPException(status_code=409, detail={
                "code": "quality_failed", "failed": failed,
                "message": "the quality report failed (%s); freezing it anyway needs an override"
                           % ", ".join(failed)})
        require_site_capability(request, current.site_id, ("override",))
        _audit("map.quality.override", current, map_id,
               {"failed": failed, "reason": body.reason, "version": report.get("version"),
                "computed_at": report.get("computed_at")})

    @router.put("/maps/{map_id}/status")
    def put_status(map_id: str, body: _StatusBody, request: Request):
        _zones_gate(map_id, body)
        _quality_gate(map_id, body, request)
        result = _curate(lambda: store.set_status(map_id, body.status, zones_reviewed=body.zones_reviewed,
                                                  zones_reviewed_by=_requested_by(request)))
        if body.zones_reviewed and body.status == "active":
            _audit("map.zones.reviewed", result, map_id,
                   {"zones": len(result.zones), "reviewed_at": result.zones_reviewed_at})
        if on_status is not None:
            try:
                on_status(store.get(map_id))
            except Exception:
                logger.exception("the map status observer failed for %s", map_id)
        return result

    # --- the map ready checklist (0.0.29): trim and rename ------------------

    @router.post("/maps/{map_id}/floors/{index}/trim")
    def post_trim(map_id: str, index: int, request: Request):
        from .trim import TRIM_MARGIN_M, TrimRefused, trim_floor

        version = store.current_version(map_id) if store.get(map_id) is not None else None
        try:
            saved = trim_floor(store, map_id, index)
        except (FloorNotFound, MapNotFound) as exc:
            detail = str(exc) if isinstance(exc, FloorNotFound) else "map not found"
            raise HTTPException(status_code=404, detail=detail) from None
        except TrimRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BundleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        now = store.current_version(map_id)
        headers = {"X-Map-Version": str(now)}
        if now != version:
            floor = next(f for f in saved.floors if f.index == index)
            _audit("map.trim", saved, map_id,
                   {"floor": index, "version": now, "margin_m": TRIM_MARGIN_M,
                    "size_cells": list(floor.size_cells), "origin_xy_m": list(floor.origin_xy_m)},
                   request=request)
            job_id = _edited(saved, now, request)
            if job_id:
                headers["X-Quality-Job"] = job_id
        return Response(saved.model_dump_json(), media_type="application/json", headers=headers)

    @router.patch("/maps/{map_id}")
    def patch_map(map_id: str, body: _RenameBody):
        labels = None if body.floors is None else {f.index: f.label for f in body.floors}
        try:
            saved = store.rename(map_id, name=body.name, labels=labels)
        except FloorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except MapNotFound:
            raise HTTPException(status_code=404, detail="map not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        _audit("map.rename", saved, map_id,
               {"name": saved.name if body.name is not None else None,
                "floors": sorted(labels) if labels is not None else []})
        return saved

    # --- quality ------------------------------------------------------------

    @router.get("/maps/{map_id}/quality")
    def get_quality(map_id: str):
        if store.get(map_id) is None:
            raise HTTPException(status_code=404, detail="map not found")
        report = store.read_quality(map_id)
        if report is None:
            raise HTTPException(status_code=404, detail="no quality report has run for this map")
        return {**report, "current": is_current(store, map_id, report)}

    @router.post("/maps/{map_id}/quality", status_code=202)
    def post_quality(map_id: str, request: Request):
        site_map = store.get(map_id)
        if site_map is None:
            raise HTTPException(status_code=404, detail="map not found")
        return _queue_quality(map_id, site_map.site_id, _requested_by(request))

    # --- versions -----------------------------------------------------------

    @router.get("/maps/{map_id}/versions")
    def list_versions(map_id: str):
        try:
            return {"items": store.list_versions(map_id)}
        except MapNotFound:
            raise HTTPException(status_code=404, detail="map not found") from None

    @router.get("/maps/{map_id}/versions/{version}/floors/{index}/{kind}")
    def get_version_artifact(map_id: str, version: int, index: int, kind: str):
        path = store.version_artifact_path(map_id, version, index, kind)
        if path is None:
            raise HTTPException(status_code=404, detail="no such file")
        # An archived version never changes, so it may be cached.
        return FileResponse(path, media_type=MEDIA_TYPES[kind], headers={"Cache-Control": "max-age=3600"})

    @router.post("/maps/{map_id}/versions/{version}/restore")
    def restore_version(map_id: str, version: int, request: Request):
        try:
            restored = store.restore_version(map_id, version)
        except VersionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except MapNotFound:
            raise HTTPException(status_code=404, detail="map not found") from None
        except BundleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _audit("map.restore", restored, map_id,
               {"restored_from": version, "version": store.current_version(map_id)})
        _queue_quality_quietly(restored, request)
        return restored

    # --- surveys and jobs ---------------------------------------------------

    def _survey_request(fields: Dict[str, str], cloud_name: str, track_name: str):
        from .survey import SurveyRequest, slug

        def number(key: str, default, kind):
            raw = fields.get(key)
            if raw is None or raw == "":
                return default
            try:
                return kind(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="%s must be a number" % key) from None

        name = fields.get("name", "").strip()
        survey_id = fields.get("survey_id")
        survey = SurveyRequest(
            survey_id=survey_id, floor_label=fields.get("floor_label", "").strip() or None,
            map_id=fields.get("map_id", "").strip() or "map-" + slug(name),
            site_id=fields.get("site_id", "").strip(), name=name,
            floor_index=number("floor", 0, int), resolution_m=number("resolution_m", 0.05, float),
            track_frame=fields.get("track_frame", "").strip() or "auto",
            cloud_name=os.path.basename(cloud_name or "cloud.pcd")[:200],
            track_name=os.path.basename(track_name or "track.csv")[:200])
        try:
            survey.check()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return survey

    def _copy_part(part: UploadFile, target: Path, limit: int, label: str) -> int:
        size = 0
        with open(target, "wb") as handle:
            while True:
                chunk = part.file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise _TooLarge("the %s exceeds %d bytes" % (label, limit))
                handle.write(chunk)
        return size

    def _survey_robot(request: Request, site_id: str, claimed: Optional[str]) -> Optional[str]:
        """The robot a survey upload is from: a device key's own robot (a
        `robot_id` field naming another is a 400), else the field, which must
        name a robot registered at the survey's site; None when not sent."""
        claimed = claimed.strip() if claimed is not None else None
        device = _device_of(request)
        if device is not None:
            if claimed and claimed != device:
                raise HTTPException(status_code=400,
                                    detail="robot_id must be this device's own robot (%s)" % device)
            return device
        if not claimed:
            return None
        if robot_site_of is not None:
            try:
                robot_site = robot_site_of(claimed)
            except Exception:
                logger.exception("could not look up the site of robot %s", claimed)
                robot_site = None
            if robot_site != site_id:
                raise HTTPException(status_code=400,
                                    detail="robot_id %s is not a robot at site %s" % (claimed, site_id))
        return claimed

    @router.post("/map-surveys", status_code=202)
    async def upload_survey(request: Request):
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_survey_bytes:
            raise HTTPException(status_code=413, detail="upload exceeds %d bytes" % max_survey_bytes)
        received = 0

        async def counting_receive():
            nonlocal received
            message = await request.receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_survey_bytes:
                    raise HTTPException(status_code=413, detail="upload exceeds %d bytes" % max_survey_bytes)
            return message

        form = await Request(request.scope, counting_receive).form(max_files=len(SURVEY_FILES),
                                                                    max_fields=len(SURVEY_FIELDS))
        try:
            files: Dict[str, UploadFile] = {}
            fields: Dict[str, str] = {}
            for name, part in form.multi_items():
                if name in files or name in fields:
                    raise HTTPException(status_code=400, detail="%r was sent twice" % name)
                if name in SURVEY_FILES:
                    if not isinstance(part, UploadFile):
                        raise HTTPException(status_code=400, detail="%r must be a file part" % name)
                    files[name] = part
                elif name in SURVEY_FIELDS:
                    if isinstance(part, UploadFile):
                        raise HTTPException(status_code=400, detail="%r must be a text field" % name)
                    fields[name] = part
                else:
                    raise HTTPException(status_code=400, detail="%r is not part of a survey upload" % name)
            for name in ("cloud", "track"):
                if name not in files:
                    raise HTTPException(status_code=400, detail="the survey needs a %r file part" % name)
            survey = _survey_request(fields, files["cloud"].filename, files["track"].filename)
            require_site_capability(request, survey.site_id, ("configure",))
            survey.robot_id = _survey_robot(request, survey.site_id, fields.get("robot_id"))
            existing = store.get(survey.map_id)
            if existing is not None and existing.site_id != survey.site_id:
                require_site_capability(request, existing.site_id, ("configure",))

            job_id = jobs.new_id()
            work_dir = jobs.work_dir(job_id)

            def stage_inputs() -> bytes:
                _copy_part(files["cloud"], work_dir / "cloud.pcd", max_survey_bytes, "cloud")
                track_path = work_dir / "track.csv"
                _copy_part(files["track"], track_path, MAX_TRACK_BYTES, "track")
                if "moved" in files:
                    _copy_part(files["moved"], work_dir / ARTIFACT_FILES["moved"], MAX_MOVED_BYTES, "moved layer")
                if "moved_cloud" in files:
                    _copy_part(files["moved_cloud"], work_dir / ARTIFACT_FILES["moved_cloud"], MAX_CLOUD_BYTES,
                               "moved cloud")
                return track_path.read_bytes()

            try:
                track_bytes = await run_in_threadpool(stage_inputs)
            except _TooLarge as exc:
                shutil.rmtree(work_dir, ignore_errors=True)
                raise HTTPException(status_code=413, detail=str(exc)) from None
            except BaseException:
                shutil.rmtree(work_dir, ignore_errors=True)
                raise

            def work(progress):
                from .survey import process_survey
                moved = work_dir / ARTIFACT_FILES["moved"]
                moved_cloud = work_dir / ARTIFACT_FILES["moved_cloud"]
                return process_survey(store, survey, work_dir / "cloud.pcd", track_bytes, work_dir,
                                      progress, detail={"job_id": job_id},
                                      moved_path=moved if moved.is_file() else None,
                                      moved_cloud_path=moved_cloud if moved_cloud.is_file() else None)

            job = jobs.submit("survey", survey.map_id, survey.site_id, work,
                              requested_by=_requested_by(request), job_id=job_id,
                              detail={"name": survey.name, "floor": survey.floor_index,
                                      "cloud": survey.cloud_name, "track": survey.track_name,
                                      "moved": "moved" in files, "replaces": existing is not None,
                                      "survey_id": survey.survey_id, "robot_id": survey.robot_id})
            _audit("map.survey", existing, survey.map_id,
                   {"job_id": job_id, "floor": survey.floor_index,
                    "cloud": survey.cloud_name, "replaces": existing is not None,
                    "survey_id": survey.survey_id, "robot_id": survey.robot_id},
                   site_id=survey.site_id, request=request)
            return job
        finally:
            await form.close()

    @router.get("/map-jobs")
    def list_jobs(request: Request, map_id: Optional[str] = None):
        if map_id is not None and not valid_map_id(map_id):
            return {"items": []}
        return {"items": [j for j in jobs.list(map_id) if site_visible(request, j.get("site_id"))]}

    @router.get("/map-jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        job = jobs.get(job_id)
        if job is None or not site_visible(request, job.get("site_id")):
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @router.delete("/maps/{map_id}", status_code=204)
    def delete_map(map_id: str):
        existing = store.get(map_id)
        if not store.delete(map_id):
            raise HTTPException(status_code=404, detail="map not found")
        _audit("map.delete", existing, map_id, {"name": existing.name if existing is not None else None})
        return Response(status_code=204)

    return router
