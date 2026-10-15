"""Run record endpoints (run-records spec §7.2). No auth: any caller can read
or write any run, like every endpoint today."""
from __future__ import annotations

from typing import Callable, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from shared.schemas.runs import MissionRun, RunEvent

from .store import RunStore

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

    return router
