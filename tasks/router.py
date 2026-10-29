"""HTTP API for mission tasks and their occurrences (2026-10-23).

The console's mission calendar reads the queue from here instead of computing
one in the browser: tasks are what an operator authors, occurrences are what the
scheduler (tasks/scheduler.py) materialised from them. Storage is tasks/store.py
-- a directory of JSON files, re-read on every request -- and the shape of this
file is deliberately the mission library's (missions/router.py).

What this is not: a runtime (POST /tasks/{id}/run-now only *queues*; nothing can
start a mission on the edge yet), or access-controlled (no auth exists anywhere;
any caller can overwrite or delete any task).

Endpoints:
- GET    /tasks[?site_id=&mission_id=]            -> {"items": [task, ...]}, by task_id
- GET    /tasks/occurrences?from=&to=[&site_id=]  -> {"items": [occurrence, ...]}, by due_at
- GET    /tasks/{id}                              -> task, 404 if unknown
- PUT    /tasks/{id}                              -> create or replace; 409 on a stale version
- DELETE /tasks/{id}                              -> 204, 404 if unknown; also deletes the task's
                                                    unstarted occurrences (history is kept) and logs both
- POST   /tasks/{id}/run-now                      -> 202 with the queued occurrence
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timezone
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.common import now_iso, read_bounded_body
from missions.router import MissionStore
from runs.dispatch import RunDispatcher, StartRefused
from shared.schemas.runs import RunNowRequest
from shared.schemas.tasks import MissionSchedule, MissionTask, TaskOccurrence
from tasks.scheduler import materialise, parse_start
from tasks.store import TaskStore, parse_utc

logger = logging.getLogger("roverhub.tasks")

# A task is a few hundred bytes of fields plus, at most, a list of skip dates.
# 64 KB is far beyond a realistic one while still bounding what an
# unauthenticated caller can write per file.
MAX_TASK_BYTES = 64 * 1024

# GET /tasks/occurrences is the calendar's listing and is declared before
# GET /tasks/{task_id}, so a task stored under this id could never be read back.
RESERVED_TASK_IDS = frozenset({"occurrences"})

# A '+' in a query string decodes to a space, so `from=...T09:00:00+09:00` --
# valid, and what a hand-written URL naturally contains -- arrives here as
# "...T09:00:00 09:00". Anchored on a full time-of-day immediately before the
# space and an offset immediately after, to the end: a *date-time* separated by
# a space ("2026-10-24 06:00") has no time before its space and is left alone.
_PLUS_DECODED_TO_SPACE = re.compile(
    r"(T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?) (\d{2}(?::?\d{2})?)$"
)


def _bound(name: str, raw: str) -> str:
    """A `from`/`to` query value, repaired if a '+' offset was decoded to a space,
    and checked so a bad one is answered with *which* bound and why."""
    repaired = _PLUS_DECODED_TO_SPACE.sub(r"\1+\2", raw)
    if parse_utc(repaired) is None:
        raise HTTPException(status_code=400, detail=(
            f"{name!r} is not an ISO 8601 timestamp: {raw!r}. "
            f"A '+' UTC offset must be percent-encoded as %2B in a URL."
        ))
    return repaired


def _schedule_problems(schedule: MissionSchedule) -> List[dict]:
    """What the schema cannot check, in the shape of a pydantic 422 entry.

    This lives here and not in shared/schemas/tasks.py on purpose. Telling a
    real timezone from a typo needs the tz database, i.e. a `tzdata` runtime
    requirement, and `shared/` must not inherit one: its TypeScript mirror and
    the edge devices do not need it. `until` and `skip_dates` are validated in
    the schema precisely because they needed no dependency -- the asymmetry is
    a decision, not an oversight.

    Without this, `Asia/Seol` is accepted and the task silently never fires: the
    scheduler logs a warning and produces no occurrence.
    """
    problems = []
    try:
        ZoneInfo(schedule.timezone)
    except (KeyError, ValueError, OSError) as exc:
        # ZoneInfoNotFoundError is a KeyError; a malformed key ("", "../x") is a
        # ValueError; a directory of the tz database ("America") raises
        # IsADirectoryError, an OSError, which is easy to forget.
        problems.append({
            "loc": ["body", "schedule", "timezone"],
            "msg": f"{schedule.timezone!r} is not an IANA timezone name such as 'Asia/Seoul' ({type(exc).__name__})",
            "type": "value_error",
        })
    try:
        parse_start(schedule.start_at)  # the one definition of what start_at means
    except ValueError as exc:
        problems.append({
            "loc": ["body", "schedule", "start_at"],
            "msg": f"{exc}; it is a wall-clock time with no offset, e.g. '2026-10-24T09:00'",
            "type": "value_error",
        })
    return problems


def build_tasks_router(
    tasks_dir: str,
    clock: Callable[[], str] = now_iso,
    scheduler_enabled: Callable[[], bool] = lambda: True,
    dispatcher: Optional[RunDispatcher] = None,
    mission_store: Optional[MissionStore] = None,
) -> APIRouter:
    """`clock` returns the UTC ISO string stamped into `updated_at` and used as
    the due time of a run-now; injectable so both are testable.

    `scheduler_enabled` is asked on each run-now, not once at build time, because
    whether the loop runs is decided at app startup (SCHEDULER_ENABLED) and this
    router is built at import. It only changes what run-now *says*: a queued
    occurrence with no loop to decide it would otherwise be promised a tick that
    never comes."""
    router = APIRouter()
    # The app builds the dispatcher after this router, so it may set the
    # attribute later; run-now reads it per request.
    router.dispatcher = dispatcher  # type: ignore[attr-defined]
    store = TaskStore(tasks_dir)

    @router.get("/tasks")
    def list_tasks(site_id: Optional[str] = None, mission_id: Optional[str] = None):
        tasks = store.list_tasks(site_id)
        if mission_id is not None:
            tasks = [t for t in tasks if t.mission_id == mission_id]
        return {"items": [t.model_dump(mode="json") for t in tasks]}

    # Declared before /tasks/{task_id}, or "occurrences" would be read as an id.
    # `from` is a Python keyword, hence the alias.
    @router.get("/tasks/occurrences")
    def list_occurrences(
        from_: str = Query(alias="from"),
        to: str = Query(),
        site_id: Optional[str] = None,
    ):
        occurrences = store.list_occurrences(site_id, _bound("from", from_), _bound("to", to))
        return {"items": [o.model_dump(mode="json") for o in occurrences]}

    @router.get("/tasks/{task_id}")
    def get_task(task_id: str):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task.model_dump(mode="json")

    # The body is read by hand rather than declared as a parameter so the size
    # cap applies before pydantic parses anything.
    @router.put("/tasks/{task_id}")
    async def put_task(task_id: str, request: Request):
        if task_id in RESERVED_TASK_IDS:
            raise HTTPException(status_code=400, detail=f"{task_id!r} is reserved and cannot be a task_id")
        store.get_task(task_id)  # validates the id (400) before the body is read
        raw = await read_bounded_body(request, MAX_TASK_BYTES, "task")
        try:
            task = MissionTask.model_validate_json(raw)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=json.loads(exc.json(include_url=False))
            ) from None
        if task.task_id != task_id:
            raise HTTPException(status_code=400, detail="body task_id does not match the path")
        if task.schedule is not None:
            problems = _schedule_problems(task.schedule)
            if problems:
                raise HTTPException(status_code=422, detail=problems)

        # Compare and write with no await between them: this handler runs on the
        # event loop, so no other request can interleave. The store itself has
        # no locking.
        stored = store.get_task(task_id)
        if stored is not None and stored.version != task.version:
            # The stored entity, so the console can show what changed.
            return JSONResponse(status_code=409, content=stored.model_dump(mode="json"))
        # A create is always version 1, whatever the body carried: it is the first
        # version, and a reader of the file expects that. Only a write to an
        # existing task increments. (There is nothing stored to compare a create
        # against, so a stale client re-creating a deleted task starts over.)
        version = 1 if stored is None else task.version + 1
        stamp = clock()
        task = task.model_copy(update={"updated_at": stamp, "version": version})
        store.put_task(task)
        _replan(task, stamp)
        return task.model_dump(mode="json")

    def _replan(task: MissionTask, stamp: str) -> None:
        # The runs already planned were worked out from the task as it was: an
        # edit that moves the time or changes the robot would otherwise leave
        # the old runs on the calendar, under the old robot, until they came due.
        # Plan again now, with the scheduler's own materialise, so the calendar
        # is right the moment the save returns rather than on the next tick.
        # The scheduler's loop never overwrites, so the two cannot disagree.
        now = parse_utc(stamp)
        if now is None:
            return
        dropped = store.drop_planned_occurrences(task.task_id, now)
        added = []
        for occ in materialise([task], now):
            if store.get_occurrence(occ.occurrence_id) is None:
                store.put_occurrence(occ)
                added.append(occ.occurrence_id)
        if dropped:
            # WARNING, like the delete cascade: deleting files must not be silent.
            logger.warning(
                "task %s changed: replaced %d planned occurrence(s) %s with %d %s",
                task.task_id, len(dropped), dropped, len(added), added,
            )

    @router.delete("/tasks/{task_id}", status_code=204)
    def delete_task(task_id: str):
        # Runs even for a task that is already gone, so a retry (or a delete of
        # an id whose file was removed some other way) still clears the unstarted
        # occurrences it left behind. The 404 contract is unchanged.
        result = store.delete_task_cascade(task_id)
        if result.occurrences_deleted or result.occurrences_kept:
            # WARNING, not INFO: nothing configures logging, so INFO is dropped,
            # and a cascade that deletes files should never be invisible.
            logger.warning(
                "deleted task %s: removed %d unstarted occurrence(s) %s; kept %d that "
                "started or were decided %s",
                task_id, len(result.occurrences_deleted), result.occurrences_deleted,
                len(result.occurrences_kept), result.occurrences_kept,
            )
        if not result.task_deleted:
            raise HTTPException(status_code=404, detail="task not found")
        return Response(status_code=204)

    @router.post("/tasks/{task_id}/run-now", status_code=202)
    def run_now(task_id: str, body: Optional[RunNowRequest] = Body(default=None)):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status != "enabled":
            # The scheduler cancels an occurrence whose task is not enabled, so
            # queueing one would only produce a cancelled record.
            raise HTTPException(status_code=409, detail=(
                f"task is {task.status}; only an enabled task can be run"
            ))
        now = parse_utc(clock()).astimezone(timezone.utc)
        due_at = f"{now:%Y-%m-%dT%H:%M:%SZ}"
        occurrence_id = f"{task.task_id}-{now:%Y%m%dT%H%M%SZ}"
        # Same id scheme as the scheduler's, to the second: a double click, or a
        # run-now landing on a slot the schedule already materialised, is the
        # same occurrence, not a second one.
        if body is not None:
            return _start_now(task, body, occurrence_id, due_at)
        existing = store.get_occurrence(occurrence_id)
        if existing is not None:
            return JSONResponse(status_code=202, content=existing.model_dump(mode="json"))
        occurrence = TaskOccurrence(
            occurrence_id=occurrence_id, task_id=task.task_id, mission_id=task.mission_id,
            site_id=task.site_id, robot_id=task.robot_id, due_at=due_at, status="due",
            # Not a claim a run started: this only queues. With the loop on, its
            # next tick decides and may skip it (no executor, robot offline, ...),
            # and its verdict replaces this message.
            message=(
                "Run-now requested: queued, not started. The scheduler decides on "
                "its next tick and may skip it; status and skip_reason will say which."
                if scheduler_enabled() else
                "Run-now requested: queued, not started. The scheduler loop is "
                "disabled on this backend, so nothing will decide this occurrence "
                "until it is enabled."
            ),
        )
        store.put_occurrence(occurrence)
        return JSONResponse(status_code=202, content=occurrence.model_dump(mode="json"))

    def _start_now(task: MissionTask, body: RunNowRequest, occurrence_id: str, due_at: str):
        """Run now with the program's steps: start on the robot at once. The
        occurrence is stored only once the dispatcher accepted, so a refusal
        leaves nothing on the calendar."""
        active = router.dispatcher  # type: ignore[attr-defined]
        if active is None or mission_store is None:
            raise HTTPException(status_code=503, detail="starting runs is not configured")
        if not task.robot_id:
            raise HTTPException(status_code=409, detail="task has no robot assigned")
        mission = mission_store.get(task.mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail="mission not found")
        try:
            run, command = active.start(
                task.robot_id, mission, body.steps, source="run_now", started_by=body.started_by,
                max_duration_s=task.max_duration_min * 60 if task.max_duration_min else None,
                run_id=occurrence_id)
        except StartRefused as refused:
            raise HTTPException(status_code=refused.status, detail=refused.message)
        occurrence = TaskOccurrence(
            occurrence_id=occurrence_id, task_id=task.task_id, mission_id=task.mission_id,
            site_id=task.site_id, robot_id=task.robot_id, due_at=due_at, status="running",
            run_id=occurrence_id, message="Run-now: started on the robot.",
        )
        store.put_occurrence(occurrence)
        return JSONResponse(status_code=201, content={
            "occurrence": occurrence.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "command": command.model_dump(mode="json"),
        })

    return router
