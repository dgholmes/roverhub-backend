"""Survey replay endpoints (survey replay spec §5.5).

The device's survey pipeline writes a survey's record, its takes and their
files with its device key, at its own robot's site (auth/policy.py). The
console reads them. Who may do what is auth/policy.py's; the handlers check
only what the guard cannot see -- the site a body names.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from auth.deps import principal_of, require_site_capability, visible
from shared.schemas.survey_replay import ReplayTake, SurveyDetail, SurveyRecord

from .store import CONTENT_TYPES, ORIGINAL_TAKE, SurveyStore

# What a device write needs at the survey's site (policy.py's rule for them).
_WRITE = ("edgeTools",)


def build_surveys_router(surveys_dir: str) -> APIRouter:
    router = APIRouter()
    store = SurveyStore(surveys_dir)
    router.store = store  # type: ignore[attr-defined]  # the app's site resolver reads it

    def _require(survey_id: str) -> SurveyRecord:
        record = store.get(survey_id)
        if record is None:
            raise HTTPException(status_code=404, detail="survey not found")
        return record

    def _own_robot(http: Request, robot_id: str) -> None:
        # A device key writes only its own robot's surveys. The guard holds it
        # to the robot of a stored survey; this covers a new one.
        principal = principal_of(http)
        if principal is not None and principal.kind == "device" and principal.id != robot_id:
            raise HTTPException(status_code=403, detail=f"this device key belongs to {principal.id}")

    @router.get("/surveys")
    def list_surveys(http: Request, site_id: Optional[str] = None, robot_id: Optional[str] = None):
        items = visible(http, store.list(site_id, robot_id), lambda s: s.site_id)
        return {"items": [s.model_dump(mode="json") for s in items]}

    @router.get("/surveys/{survey_id}")
    def get_survey(survey_id: str):
        record = _require(survey_id)
        return SurveyDetail(record=record, takes=store.takes(survey_id)).model_dump(mode="json")

    @router.put("/surveys/{survey_id}")
    def put_survey(survey_id: str, record: SurveyRecord, http: Request):
        store.survey_dir(survey_id)
        if record.survey_id != survey_id:
            raise HTTPException(status_code=400, detail="body survey_id does not match the path")
        # The guard checked the stored record's site; this is the body's.
        require_site_capability(http, record.site_id, _WRITE)
        _own_robot(http, record.robot_id)
        store.put(record)
        return record.model_dump(mode="json")

    @router.put("/surveys/{survey_id}/takes/{take_id}")
    def put_take(survey_id: str, take_id: str, take: ReplayTake, http: Request):
        store.take_dir(survey_id, take_id)
        if take.survey_id != survey_id or take.take_id != take_id:
            raise HTTPException(status_code=400, detail="body survey_id or take_id does not match the path")
        record = _require(survey_id)
        require_site_capability(http, record.site_id, _WRITE)
        store.put_take(take)
        return take.model_dump(mode="json")

    @router.delete("/surveys/{survey_id}/takes/{take_id}")
    def delete_take(survey_id: str, take_id: str, http: Request):
        if take_id == ORIGINAL_TAKE:
            raise HTTPException(status_code=400, detail="the original take cannot be deleted")
        record = _require(survey_id)
        require_site_capability(http, record.site_id, _WRITE)
        if not store.delete_take(survey_id, take_id):
            raise HTTPException(status_code=404, detail="take not found")
        return {"deleted": take_id}

    @router.put("/surveys/{survey_id}/takes/{take_id}/files/{name}")
    async def put_file(survey_id: str, take_id: str, name: str, request: Request):
        store.file_path(survey_id, take_id, name)  # validate before reading the body
        record = _require(survey_id)
        # The take's metadata comes first: a file for a take nobody declared is
        # refused, as a take for a survey with no record is.
        if store.get_take(survey_id, take_id) is None:
            raise HTTPException(status_code=404, detail="take not found")
        require_site_capability(request, record.site_id, _WRITE)
        declared = request.headers.get("content-length")
        size = await store.write_file(survey_id, take_id, name, request.stream(),
                                      int(declared) if declared and declared.isdigit() else None)
        return {"name": name, "size_bytes": size}

    @router.get("/surveys/{survey_id}/takes/{take_id}/files/{name}")
    def get_file(survey_id: str, take_id: str, name: str, request: Request):
        path = store.file_path(survey_id, take_id, name)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="file not found")
        size = os.path.getsize(path)
        media_type = CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        # Single byte ranges, served by hand as runs/router.py does (Starlette's
        # FileResponse ignores Range): the console fetches cloud.bin in pieces.
        header = request.headers.get("range")
        start, end = 0, size - 1
        status = 200
        if header and header.startswith("bytes="):
            first, _, last = header[6:].split(",")[0].partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:
                    start = max(0, size - int(last))
            except ValueError:
                raise HTTPException(status_code=416, detail="bad range") from None
            end = min(end, size - 1)
            if start > end or start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206

        def _iter():
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(_iter(), status_code=status, media_type=media_type, headers=headers)

    return router
