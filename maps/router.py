"""Map endpoints: upload a packed survey, list and read maps, serve their files.

- GET    /maps[?site_id=]                       -> {"items": [SiteMap, ...]}
- GET    /maps/{map_id}                         -> SiteMap, 404 if unknown
- POST   /maps                                  -> 201 + SiteMap; replaces any map with that id
- GET    /maps/{map_id}/floors/{index}/{kind}   -> the floor's `grid` (PGM), `preview`
                                                   (PNG) or `cloud` (float32 xyz); 404 if
                                                   there is no such file, 413 if a cloud is
                                                   over 8 MiB
- GET    /maps/{map_id}/floors/{index}/planning -> the floor's planning grid (PGM), the map
                                                   Nav2 plans on; 404 if it has none
- PUT    /maps/{map_id}/floors/{index}/planning -> SiteMap; the raw PGM as the body
         [?clearance_m=0.2]                        sets it (floor.planning gets its counts);
                                                   400 if it is not a P5 PGM of exactly the
                                                   floor's size_cells, 404 for an unknown
                                                   map or floor, 413 over 32 MiB
- DELETE /maps/{map_id}/floors/{index}/planning -> SiteMap without it; 404 if there is none
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
                                                   `track_frame` (auto | raw | level). The job
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

A quality job is also queued after every upload, restore and planning-grid
change, so the console usually finds a fresh report waiting. PUT status
`active` refuses with 409 unless the report is current (detail.code
`quality_missing` / `quality_stale`) and passed (`quality_failed`, with
`failed`); `{"status": "active", "override_quality": true, "reason": "..."}`
freezes a failed one for a caller holding `override` at the map's site, and
the audit log records who, why, and which checks failed.

POST takes the bundle tools/map_prep/pack_map.py writes as multipart form data,
one file part per bundle file, **the part's field name being the file's path in
the bundle**:

    curl -F site_map.json=@site_map.json -F floor0/grid.pgm=@floor0/grid.pgm \\
         -F floor0/grid.png=@floor0/grid.png -F floor0/cloud.bin=@floor0/cloud.bin \\
         http://localhost:8000/maps

A floor's `floor<N>/planning.pgm` may be sent as well; it is optional. So may
`floor<N>/track.csv`, the path the survey walked in the bundle's (levelled)
frame: the quality report needs it for path coverage, drift and track tilt.

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
may walk -- so it is `editZones` too. Neither changes the map's status.
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
from typing import Callable, Dict, Optional

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
    DEFAULT_CLEARANCE_M, DEFAULT_MAPS_DIR, MAX_CLOUD_BYTES, MAX_PLANNING_BYTES, MAX_TRACK_BYTES, UPLOAD_PREFIX,
    BundleError, FloorNotFound, MapNotFound, MapStore, PlanningNotFound, VersionNotFound, valid_map_id,
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
    r"site_map\.json|floor[0-9]{1,3}/(?:grid\.pgm|grid\.png|cloud\.bin|planning\.pgm|track\.csv)")
MEDIA_TYPES = {"grid": "image/x-portable-graymap", "preview": "image/png",
               "cloud": "application/octet-stream", "planning": "image/x-portable-graymap",
               "track": "text/csv", "survey": "application/json"}

# A survey upload: the raw cloud is the bulk. The 2026-11-09 office survey's
# .pcd is 88 MB for 2.76 M points with normals; 1 GiB is a building ten times
# that, and survey_math.MAX_POINTS refuses more points than the server holds.
MAX_SURVEY_BYTES = 1024 * 1024 * 1024
SURVEY_FIELDS = ("site_id", "name", "map_id", "floor", "resolution_m", "track_frame")
_CHUNK = 1024 * 1024


class _TooLarge(Exception):
    pass


class _StatusBody(BaseModel):
    status: MapStatus
    # Freeze although the quality report failed. Needs `override` at the
    # map's site; `reason` goes to the audit log.
    override_quality: bool = False
    reason: Optional[str] = Field(default=None, max_length=500)


def _limit_for(name: str) -> int:
    if name == "site_map.json":
        return MAX_MANIFEST_BYTES
    if name.endswith("cloud.bin"):
        return MAX_CLOUD_BYTES
    if name.endswith("track.csv"):
        return MAX_TRACK_BYTES
    return MAX_IMAGE_BYTES


def build_maps_router(maps_dir: str = DEFAULT_MAPS_DIR,
                      max_upload_bytes: int = MAX_UPLOAD_BYTES,
                      on_status: Optional[Callable[[SiteMap], None]] = None,
                      audit: Optional[AuditFn] = None,
                      jobs_dir: Optional[str] = None,
                      start_jobs: bool = True,
                      max_survey_bytes: int = MAX_SURVEY_BYTES) -> APIRouter:
    """`on_status(map)` after a status change is stored (the audit log).
    `audit` (history/audit.py AuditRecorder.record) is told every upload,
    zone edit, planning-grid change, delete, restore and quality override.

    Background jobs keep their records in `jobs_dir`, by default `map-jobs`
    beside `maps_dir` (outside the store, whose directory names are map ids).
    `start_jobs=False` (tests) queues jobs without a worker thread; call
    `router.jobs.run_pending()`."""
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

    def _audit(action: str, site_map: Optional[SiteMap], map_id: str, detail: dict,
               site_id: Optional[str] = None) -> None:
        if audit is None:
            return
        try:
            audit(action, map_id, "ok", detail,
                  site_id=site_id or (site_map.site_id if site_map is not None else None))
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
        if kind == "cloud" and os.path.getsize(path) > MAX_CLOUD_BYTES:
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

    # The body is the PGM itself, read by hand so the size cap applies before
    # anything is parsed (as for zones).
    @router.put("/maps/{map_id}/floors/{index}/planning")
    async def put_planning(map_id: str, index: int, request: Request,
                           clearance_m: float = DEFAULT_CLEARANCE_M):
        raw = await read_bounded_body(request, MAX_PLANNING_BYTES, "planning grid")
        saved = await run_in_threadpool(
            _planning, lambda: store.set_planning(map_id, index, raw, clearance_m))
        _audit("map.planning.set", saved, map_id,
               {"floor": index, "clearance_m": clearance_m, "bytes": len(raw)})
        _queue_quality_quietly(saved, request)
        return saved

    @router.delete("/maps/{map_id}/floors/{index}/planning")
    def delete_planning(map_id: str, index: int, request: Request):
        saved = _planning(lambda: store.delete_planning(map_id, index))
        _audit("map.planning.delete", saved, map_id, {"floor": index})
        _queue_quality_quietly(saved, request)
        return saved

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
        _quality_gate(map_id, body, request)
        result = _curate(lambda: store.set_status(map_id, body.status))
        if on_status is not None:
            try:
                on_status(store.get(map_id))
            except Exception:
                logger.exception("the map status observer failed for %s", map_id)
        return result

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
        survey = SurveyRequest(
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

        form = await Request(request.scope, counting_receive).form(max_files=2, max_fields=len(SURVEY_FIELDS))
        try:
            files: Dict[str, UploadFile] = {}
            fields: Dict[str, str] = {}
            for name, part in form.multi_items():
                if name in files or name in fields:
                    raise HTTPException(status_code=400, detail="%r was sent twice" % name)
                if name in ("cloud", "track"):
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
            existing = store.get(survey.map_id)
            if existing is not None and existing.site_id != survey.site_id:
                require_site_capability(request, existing.site_id, ("configure",))

            job_id = jobs.new_id()
            work_dir = jobs.work_dir(job_id)

            def stage_inputs() -> bytes:
                _copy_part(files["cloud"], work_dir / "cloud.pcd", max_survey_bytes, "cloud")
                track_path = work_dir / "track.csv"
                _copy_part(files["track"], track_path, MAX_TRACK_BYTES, "track")
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
                return process_survey(store, survey, work_dir / "cloud.pcd", track_bytes, work_dir,
                                      progress, detail={"job_id": job_id})

            job = jobs.submit("survey", survey.map_id, survey.site_id, work,
                              requested_by=_requested_by(request), job_id=job_id,
                              detail={"name": survey.name, "floor": survey.floor_index,
                                      "cloud": survey.cloud_name, "track": survey.track_name,
                                      "replaces": existing is not None})
            _audit("map.survey", existing, survey.map_id,
                   {"job_id": job_id, "floor": survey.floor_index,
                    "cloud": survey.cloud_name, "replaces": existing is not None},
                   site_id=survey.site_id)
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
