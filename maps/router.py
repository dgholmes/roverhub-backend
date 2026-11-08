"""Map endpoints: upload a packed survey, list and read maps, serve their files.

- GET    /maps[?site_id=]                       -> {"items": [SiteMap, ...]}
- GET    /maps/{map_id}                         -> SiteMap, 404 if unknown
- POST   /maps                                  -> 201 + SiteMap; replaces any map with that id
- GET    /maps/{map_id}/floors/{index}/{kind}   -> the floor's `grid` (PGM), `preview`
                                                   (PNG) or `cloud` (float32 xyz); 404 if
                                                   there is no such file, 413 if a cloud is
                                                   over 8 MiB
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

The field name, not the filename, because a filename is whatever a browser or
client chooses to send, while a field name is carried verbatim. Only the paths
the bundle layout names are accepted (`PART_NAME_RE`); anything else is a 400,
so a part can never choose where it is written. The parts are written into a
scratch directory and handed to `MapStore.save_bundle`, which validates and
stores them.

Access is the rules in auth/policy.py: reading is open to every role, uploading
or deleting a map is site configuration, and drawing zones and freezing a map
(`editZones`) is the supervisor's review of it.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from api.common import read_bounded_body
from shared.schemas.maps import MapStatus, MapZone, SiteMap

from .store import (
    DEFAULT_MAPS_DIR, MAX_CLOUD_BYTES, UPLOAD_PREFIX, BundleError, MapNotFound, MapStore,
)

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

PART_NAME_RE = re.compile(r"site_map\.json|floor[0-9]{1,3}/(?:grid\.pgm|grid\.png|cloud\.bin)")
MEDIA_TYPES = {"grid": "image/x-portable-graymap", "preview": "image/png",
               "cloud": "application/octet-stream"}
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
                      max_upload_bytes: int = MAX_UPLOAD_BYTES) -> APIRouter:
    router = APIRouter()
    store = MapStore(maps_dir)
    router.store = store  # type: ignore[attr-defined]  # tests and the app share it

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
    def list_maps(site_id: Optional[str] = None):
        return {"items": [m.model_dump(mode="json") for m in store.list()
                          if site_id is None or m.site_id == site_id]}

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
            try:
                return await run_in_threadpool(_ingest, parts)
            except _TooLarge as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from None
            except BundleError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        finally:
            await form.close()

    @router.get("/maps/{map_id}/floors/{index}/{kind}")
    def get_artifact(map_id: str, index: int, kind: str):
        path = store.artifact_path(map_id, index, kind)
        if path is None:
            raise HTTPException(status_code=404, detail="no such file")
        if kind == "cloud" and os.path.getsize(path) > MAX_CLOUD_BYTES:
            raise HTTPException(status_code=413, detail="cloud exceeds %d bytes" % MAX_CLOUD_BYTES)
        # A re-upload replaces the files under the same URL, so make the browser
        # revalidate (FileResponse supplies the ETag) rather than guess.
        return FileResponse(path, media_type=MEDIA_TYPES[kind], headers={"Cache-Control": "no-cache"})

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
        return _curate(lambda: store.save_zones(map_id, zones))

    @router.put("/maps/{map_id}/status")
    def put_status(map_id: str, body: _StatusBody):
        return _curate(lambda: store.set_status(map_id, body.status))

    @router.delete("/maps/{map_id}", status_code=204)
    def delete_map(map_id: str):
        if not store.delete(map_id):
            raise HTTPException(status_code=404, detail="map not found")
        return Response(status_code=204)

    return router
