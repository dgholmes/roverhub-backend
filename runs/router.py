"""Run record endpoints (run-records spec §7.2). No auth: any caller can read
or write any run, like every endpoint today."""
from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from typing import Callable, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response

from auth.deps import acting_user_id, require_site_capability, visible
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from shared.schemas.runs import MissionRun, RunEvent, StartRunRequest

from .dispatch import RunDispatcher, StartRefused
from .figure import NoFigure, build_scene, render_png, render_svg
from .store import CONTENT_TYPES, EVENTS_FILE, RUN_FILE, RunStore
from .track import HistorySamples, build_track

logger = logging.getLogger("roverhub.runs")

OnChange = Callable[[str, Optional[str]], None]


class EventBatch(BaseModel):
    events: List[RunEvent]


def _summary(store: RunStore, run: MissionRun) -> dict:
    files = store.files(run.run_id)
    count = lambda prefix: sum(1 for f in files if f["path"].startswith(prefix))  # noqa: E731
    # A mission's Snapshot steps save their picture in the shared snapshot store
    # and name it in the run's events (`snapshot_id`); nothing is copied into
    # the run's own photos/ folder. Counting only that folder showed 0 photos
    # for runs that took several (operator, 2026-11-26). A picture is counted
    # once, whether it is a file in the folder or an event's snapshot.
    snapshots = {e.snapshot_id for e in store.events(run.run_id) if e.snapshot_id}
    return {**run.model_dump(mode="json"), "photo_count": count("photos/") + len(snapshots),
            "video_count": count("video/"), "detection_count": count("detections/")}


def build_runs_router(runs_dir: str, on_change: OnChange = lambda run_id, path: None,
                      dispatcher: Optional[RunDispatcher] = None,
                      on_run_ended: Callable[[MissionRun], None] = lambda run: None,
                      map_store=None, history_samples: Optional[HistorySamples] = None,
                      site_timezone: Optional[Callable[[str], Optional[str]]] = None) -> APIRouter:
    """`map_store` (maps/store.py MapStore), `history_samples` and
    `site_timezone` feed the run log (/track, the figure); each may be None,
    and the run log then does without: no map, no history fallback, UTC."""
    router = APIRouter()
    # The dispatcher needs this router's store, so the app may also set the
    # attribute after building both; the route reads it per request.
    router.dispatcher = dispatcher  # type: ignore[attr-defined]
    store = RunStore(runs_dir)
    router.store = store  # type: ignore[attr-defined]  # for Tasks 5-7

    def _require(run_id: str) -> MissionRun:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @router.post("/robots/{robot_id}/runs", status_code=201)
    def start_run(robot_id: str, request: StartRunRequest, http: Request):
        if router.dispatcher is None:  # type: ignore[attr-defined]
            raise HTTPException(status_code=503, detail="starting runs is not configured")
        try:
            run, command = router.dispatcher.start(  # type: ignore[attr-defined]
                robot_id, request.mission, request.steps, source=request.source,
                started_by=acting_user_id(http, request.started_by), max_duration_s=request.max_duration_s)
        except StartRefused as refused:
            raise HTTPException(status_code=refused.status, detail=refused.message) from refused
        return {"run": run.model_dump(mode="json"), "command": command.model_dump(mode="json")}

    @router.put("/runs/{run_id}")
    def put_run(run_id: str, run: MissionRun, http: Request):
        store.run_dir(run_id)
        if run.run_id != run_id:
            raise HTTPException(status_code=400, detail="body run_id does not match the path")
        # The guard checked the stored run's site; this is the body's.
        require_site_capability(http, run.site_id, ("dispatchPatrols",))
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
        closed: Optional[MissionRun] = None
        resent: Optional[MissionRun] = None
        # One lock for the append and the close: the store dedupes by seq, so
        # "not already stored" is how a resent run_ended is told from a new one.
        with store.lock(run_id):
            seen = {e.seq for e in store.events(run_id)}
            added = store.add_events(run_id, batch.events)
            ending = next((e for e in batch.events if e.kind == "run_ended" and e.seq not in seen), None)
            run = store.get(run_id)
            # A run that already ended keeps the outcome it was first given.
            if ending is not None and run is not None and run.result == "running":
                run.result = ending.result or "failed"
                run.ended_at = ending.at
                run.end_reason = ending.reason
                store.put(run)
                closed = run
            elif run is not None and run.result != "running" and any(
                    e.kind == "run_ended" and e.seq in seen for e in batch.events):
                # A resend of a run_ended we already stored. The run keeps the
                # outcome it was first given; only the occurrence settling is
                # retried, in case the first attempt failed after the lock.
                resent = run
        if added > 0:
            on_change(run_id, EVENTS_FILE)
        if closed is not None:
            on_change(run_id, None)
        settle = closed if closed is not None else resent
        if settle is not None:
            try:
                on_run_ended(settle)
            except Exception:
                # Settling the occurrence is retried on the edge's resend; it
                # must not turn a stored event into a failed request.
                logger.exception("could not settle the occurrence of run %s", run_id)
        return {"added": added, "total": len(store.events(run_id))}

    @router.get("/runs")
    def list_runs(http: Request, site_id: Optional[str] = None, robot_id: Optional[str] = None,
                  mission_id: Optional[str] = None, since: Optional[str] = None,
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        # Only the sites the caller may see (auth/deps.py; everyone with sign-in off),
        # before paging, so `total` counts what they can page through.
        runs = visible(http, store.list(site_id, robot_id, mission_id, since), lambda r: r.site_id)
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

    def _track(run: MissionRun) -> dict:
        return build_track(run, store.events(run.run_id), store.run_dir(run.run_id),
                           history_samples=history_samples, map_store=map_store)

    def _timezone(run: MissionRun) -> Optional[str]:
        if site_timezone is None or not run.site_id:
            return None
        try:
            return site_timezone(run.site_id)
        except Exception:
            logger.exception("could not read the timezone of site %s", run.site_id)
            return None

    def _scene(run: MissionRun):
        return build_scene(run, _track(run), map_store, _timezone(run))

    @router.get("/runs/{run_id}/track")
    def get_track(run_id: str):
        return _track(_require(run_id))

    @router.get("/runs/{run_id}/figure.svg")
    def get_figure_svg(run_id: str):
        try:
            scene = _scene(_require(run_id))
        except NoFigure as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from None
        return Response(render_svg(scene), media_type="image/svg+xml")

    @router.get("/runs/{run_id}/figure.png")
    def get_figure_png(run_id: str):
        try:
            scene = _scene(_require(run_id))
        except NoFigure as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from None
        return Response(render_png(scene), media_type="image/png")

    def _figures(run: MissionRun) -> List[tuple]:
        """(name, bytes) of the figure files for the zip; none when the run
        has no map, or drawing fails -- the zip must still download."""
        try:
            scene = _scene(run)
            return [("figure.svg", render_svg(scene).encode("utf-8")), ("figure.png", render_png(scene))]
        except NoFigure:
            return []
        except Exception:
            logger.exception("could not draw the figure of run %s for its zip", run.run_id)
            return []

    @router.get("/runs/{run_id}/zip")
    def get_zip(run_id: str):
        run = _require(run_id)
        base = store.run_dir(run_id)
        figures = _figures(run)
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
                for name, data in figures:
                    z.writestr(f"{run_id}/{name}", data, compress_type=zipfile.ZIP_DEFLATED)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return FileResponse(tmp, media_type="application/zip", filename=f"{run_id}.zip",
                            background=BackgroundTask(os.unlink, tmp))

    return router
