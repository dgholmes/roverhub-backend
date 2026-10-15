"""Run record endpoints (run-records spec §7.2). No auth: any caller can read
or write any run, like every endpoint today."""
from __future__ import annotations

import os
import tempfile
import zipfile
from typing import Callable, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from shared.schemas.runs import MissionRun, RunEvent

from .store import CONTENT_TYPES, EVENTS_FILE, RUN_FILE, RunStore

OnChange = Callable[[str, Optional[str]], None]


class EventBatch(BaseModel):
    events: List[RunEvent]


def _summary(store: RunStore, run: MissionRun) -> dict:
    files = store.files(run.run_id)
    count = lambda prefix: sum(1 for f in files if f["path"].startswith(prefix))  # noqa: E731
    return {**run.model_dump(mode="json"), "photo_count": count("photos/"),
            "video_count": count("video/"), "detection_count": count("detections/")}


def build_runs_router(runs_dir: str, on_change: OnChange = lambda run_id, path: None) -> APIRouter:
    router = APIRouter()
    store = RunStore(runs_dir)
    router.store = store  # type: ignore[attr-defined]  # for Tasks 5-7

    def _require(run_id: str) -> MissionRun:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @router.put("/runs/{run_id}")
    def put_run(run_id: str, run: MissionRun):
        store.run_dir(run_id)
        if run.run_id != run_id:
            raise HTTPException(status_code=400, detail="body run_id does not match the path")
        with store.lock(run_id):
            previous = store.get(run_id)
            if previous is not None and previous.bucket_copied_at and not run.bucket_copied_at:
                run = run.model_copy(update={"bucket_copied_at": previous.bucket_copied_at})
            store.put(run)
        on_change(run_id, None)
        return run.model_dump(mode="json")

    @router.post("/runs/{run_id}/events")
    def post_events(run_id: str, batch: EventBatch):
        _require(run_id)
        if any(e.run_id != run_id for e in batch.events):
            raise HTTPException(status_code=400, detail="event run_id does not match the path")
        added = store.add_events(run_id, batch.events)
        return {"added": added, "total": len(store.events(run_id))}

    @router.get("/runs")
    def list_runs(site_id: Optional[str] = None, robot_id: Optional[str] = None,
                  mission_id: Optional[str] = None, since: Optional[str] = None,
                  limit: int = 50, offset: int = 0):
        limit = max(1, min(limit, 500))
        runs = store.list(site_id, robot_id, mission_id, since)
        page = runs[offset:offset + limit]
        return {"items": [_summary(store, r) for r in page], "total": len(runs),
                "next_offset": offset + limit if offset + limit < len(runs) else None}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str):
        run = _require(run_id)
        return {"run": run.model_dump(mode="json"), "files": store.files(run_id),
                "event_count": len(store.events(run_id))}

    @router.get("/runs/{run_id}/events")
    def get_events(run_id: str):
        _require(run_id)
        return {"items": [e.model_dump(mode="json") for e in store.events(run_id)]}

    @router.put("/runs/{run_id}/files/{rel:path}")
    async def put_file(run_id: str, rel: str, request: Request):
        store.file_path(run_id, rel)  # validate before reading the body
        _require(run_id)
        declared = request.headers.get("content-length")
        size = await store.write_file(run_id, rel, request.stream(),
                                      int(declared) if declared and declared.isdigit() else None)
        await run_in_threadpool(on_change, run_id, rel)
        return {"path": rel, "size_bytes": size}

    @router.get("/runs/{run_id}/files/{rel:path}")
    def get_file(run_id: str, rel: str, request: Request):
        path = store.file_path(run_id, rel)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="file not found")
        size = os.path.getsize(path)
        media_type = CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        # Starlette 0.36's FileResponse ignores Range; a <video> can't seek
        # without it, so single ranges are served by hand.
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
                raise HTTPException(status_code=416, detail="bad range")
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

    @router.get("/runs/{run_id}/zip")
    def get_zip(run_id: str):
        _require(run_id)
        base = store.run_dir(run_id)
        # Built into a temp file, not memory: a run can hold gigabytes of video.
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w") as z:
                for name in (RUN_FILE, EVENTS_FILE):
                    full = os.path.join(base, name)
                    if os.path.isfile(full):
                        z.write(full, f"{run_id}/{name}", compress_type=zipfile.ZIP_DEFLATED)
                for f in store.files(run_id):
                    kind = zipfile.ZIP_STORED if f["path"].startswith("video/") else zipfile.ZIP_DEFLATED
                    z.write(os.path.join(base, *f["path"].split("/")), f"{run_id}/{f['path']}", compress_type=kind)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return FileResponse(tmp, media_type="application/zip", filename=f"{run_id}.zip",
                            background=BackgroundTask(os.unlink, tmp))

    return router
