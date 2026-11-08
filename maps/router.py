"""Map endpoints: upload a packed survey, list and read maps, serve their files.

- GET    /maps[?site_id=]                       -> {"items": [SiteMap, ...]}
- GET    /maps/{map_id}                         -> SiteMap, 404 if unknown
- POST   /maps                                  -> 201 + SiteMap; replaces any map with that id
- GET    /maps/{map_id}/floors/{index}/{kind}   -> the floor's `grid` (PGM), `preview`
                                                   (PNG) or `cloud` (float32 xyz); 404 if
                                                   there is no such file, 413 if a cloud is
                                                   over 8 MiB
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

Access is the rules in auth/policy.py: reading is open to every role, changing
a map is site configuration.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from shared.schemas.maps import SiteMap

from .store import DEFAULT_MAPS_DIR, MAX_CLOUD_BYTES, BundleError, MapStore

# Per-part limits. A cloud's is the packer's own figure (see store.MAX_CLOUD_BYTES).
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
# The request as a whole: the largest bundle this accepts, one floor's worth
# of everything at its limit, with room for a second floor's grid and preview.
MAX_UPLOAD_BYTES = 128 * 1024 * 1024

PART_NAME_RE = re.compile(r"site_map\.json|floor\d{1,3}/(?:grid\.pgm|grid\.png|cloud\.bin)")
MEDIA_TYPES = {"grid": "image/x-portable-graymap", "preview": "image/png",
               "cloud": "application/octet-stream"}
_CHUNK = 1024 * 1024


class _TooLarge(Exception):
    pass


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
        with tempfile.TemporaryDirectory(dir=store.root, prefix=".upload-") as scratch:
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
        form = await request.form()
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

    @router.delete("/maps/{map_id}", status_code=204)
    def delete_map(map_id: str):
        if not store.delete(map_id):
            raise HTTPException(status_code=404, detail="map not found")
        return Response(status_code=204)

    return router
