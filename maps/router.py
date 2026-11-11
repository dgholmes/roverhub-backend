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

POST takes the bundle tools/map_prep/pack_map.py writes as multipart form data,
one file part per bundle file, **the part's field name being the file's path in
the bundle**:

    curl -F site_map.json=@site_map.json -F floor0/grid.pgm=@floor0/grid.pgm \\
         -F floor0/grid.png=@floor0/grid.png -F floor0/cloud.bin=@floor0/cloud.bin \\
         http://localhost:8000/maps

A floor's `floor<N>/planning.pgm` may be sent as well; it is optional.

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
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
import logging
from typing import Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from api.common import read_bounded_body
from auth.deps import require_site_capability, site_visible
from history.audit import AuditFn
from shared.schemas.maps import MapStatus, MapZone, SiteMap

from .store import (
    DEFAULT_CLEARANCE_M, DEFAULT_MAPS_DIR, MAX_CLOUD_BYTES, MAX_PLANNING_BYTES, UPLOAD_PREFIX,
    BundleError, FloorNotFound, MapNotFound, MapStore, PlanningNotFound,
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
    r"site_map\.json|floor[0-9]{1,3}/(?:grid\.pgm|grid\.png|cloud\.bin|planning\.pgm)")
MEDIA_TYPES = {"grid": "image/x-portable-graymap", "preview": "image/png",
               "cloud": "application/octet-stream", "planning": "image/x-portable-graymap"}
_CHUNK = 1024 * 1024


class _TooLarge(Exception):
    pass


class _StatusBody(BaseModel):
    status: MapStatus


def _limit_for(name: str) -> int:
    if name == "site_map.json":
        return MAX_MANIFEST_BYTES
    if name.endswith("cloud.bin"):
        return MAX_CLOUD_BYTES
    return MAX_IMAGE_BYTES


def build_maps_router(maps_dir: str = DEFAULT_MAPS_DIR,
                      max_upload_bytes: int = MAX_UPLOAD_BYTES,
                      on_status: Optional[Callable[[SiteMap], None]] = None,
                      audit: Optional[AuditFn] = None) -> APIRouter:
    """`on_status(map)` after a status change is stored (the audit log).
    `audit` (history/audit.py AuditRecorder.record) is told every upload,
    zone edit, planning-grid change and delete."""
    router = APIRouter()
    store = MapStore(maps_dir)
    router.store = store  # type: ignore[attr-defined]  # tests and the app share it

    def _audit(action: str, site_map: Optional[SiteMap], map_id: str, detail: dict) -> None:
        if audit is None:
            return
        try:
            audit(action, map_id, "ok", detail, site_id=site_map.site_id if site_map is not None else None)
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
                return saved
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
        return saved

    @router.delete("/maps/{map_id}/floors/{index}/planning")
    def delete_planning(map_id: str, index: int):
        saved = _planning(lambda: store.delete_planning(map_id, index))
        _audit("map.planning.delete", saved, map_id, {"floor": index})
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

    @router.put("/maps/{map_id}/status")
    def put_status(map_id: str, body: _StatusBody):
        result = _curate(lambda: store.set_status(map_id, body.status))
        if on_status is not None:
            try:
                on_status(store.get(map_id))
            except Exception:
                logger.exception("the map status observer failed for %s", map_id)
        return result

    @router.delete("/maps/{map_id}", status_code=204)
    def delete_map(map_id: str):
        existing = store.get(map_id)
        if not store.delete(map_id):
            raise HTTPException(status_code=404, detail="map not found")
        _audit("map.delete", existing, map_id, {"name": existing.name if existing is not None else None})
        return Response(status_code=204)

    return router
