from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auth.deps import Auth
from auth.mqtt_hooks import build_mqtt_hooks_router
from auth.router import build_auth_router
from control.commands import CommandStore
from control.media import DEFAULT_AUDIO_CLIPS_DIR, MAX_AUDIO_CLIP_BYTES, ContentLengthLimitMiddleware, build_media_router
from control.snapshots import DEFAULT_SNAPSHOTS_DIR, build_snapshot_router
from control.router import build_router
from maps.router import DEFAULT_MAPS_DIR, build_maps_router
from missions.router import DEFAULT_MISSIONS_DIR, MissionStore, build_missions_router
from realtime.mqtt_client import BackendMqttClient, real_mqtt_client_factory
from robots.registry import RobotRegistry, offline_after_s_from_env
from robots.router import build_robots_router
from runs.dispatch import RunDispatcher, make_run_ended_hook
from runs.bucket import from_env as bucket_from_env
from runs.reaper import RunReaper
from runs.reconcile import make_state_hook
from runs.router import build_runs_router
from runs.store import DEFAULT_RUNS_DIR
from tasks.router import build_tasks_router
from tasks.scheduler import EXECUTOR_KINDS, SchedulerLoop, has_spoken_steps
from tasks.store import DEFAULT_TASKS_DIR, TaskStore
from shared.schemas.missions import PROGRAM_COMPILER_VERSION
from shared.schemas.runs import MissionRun
from shared.schemas.tasks import TaskOccurrence

logger = logging.getLogger("roverhub.tasks")

load_dotenv()  # loads .env in the working directory, if present; real env vars still win

# Device acceptance is informational and defaults to "auto" -- every bridge
# that registers is accepted, as before. See robots/registry.py for why it is
# not (and cannot yet be) a gate. DEVICE_ACCEPTANCE=manual makes unknown
# devices arrive "pending"; ACCEPTED_DEVICES pre-accepts robot or bridge ids.
registry = RobotRegistry(
    acceptance_mode=os.environ.get("DEVICE_ACCEPTANCE", "auto").strip().lower() or "auto",
    preaccepted=os.environ.get("ACCEPTED_DEVICES", "").split(","),
    # A robot unheard from for ROBOT_OFFLINE_AFTER_S (default 15 s, three
    # heartbeats) reads as offline; dispatch and the scheduler refuse it.
    offline_after_s=offline_after_s_from_env(),
)
command_store = CommandStore()
_reconcile_hook = None  # bound below, once the run and task stores exist


def _on_state(robot_id, previous_boot_id, boot_id):
    if _reconcile_hook is not None:
        _reconcile_hook(robot_id, previous_boot_id, boot_id)


_run_dispatcher = None  # bound below, once the run store exists


def _on_ack(command):
    command_store.apply_ack(command)
    # A START_MISSION the bridge refused fails its run (runs/dispatch.py). Kept
    # apart from apply_ack so a dispatcher error cannot lose the ack itself.
    if _run_dispatcher is not None:
        try:
            _run_dispatcher.on_ack(command)
        except Exception:
            logger.exception("could not apply the ack for command %s to its run", command.command_id)


mqtt_client = BackendMqttClient(
    os.environ.get("CLOUD_MQTT_URL", "mqtt://localhost:1883"), registry, real_mqtt_client_factory,
    on_ack=_on_ack, on_state=_on_state,
)


def _resolve_site(params):
    """The (site_id, robot_id) a request's path refers to, for role checks.
    Bound late: the stores it reads are built further down this module."""
    try:
        if "robot_id" in params:
            record = registry.get(params["robot_id"])
            return (record.site_id if record else None), params["robot_id"]
        if "run_id" in params:
            run = runs_router.store.get(params["run_id"])
            return (run.site_id, run.robot_id) if run else (None, None)
        if "task_id" in params:
            task = _reconcile_task_store.get_task(params["task_id"])
            return (task.site_id, task.robot_id) if task else (None, None)
        if "mission_id" in params:
            mission = _scheduler_missions.get(params["mission_id"])
            return (getattr(mission, "site_id", None) if mission else None), None
    except NameError:
        pass  # a request during startup, before the stores exist
    return None, None


# Sign-in (auth/). Off unless AUTH_ENABLED=true. The guard is an app-wide
# dependency, so every route -- including ones added later -- is checked
# against auth/policy.py, and a route with no rule there is refused.
auth = Auth.from_env(resolve_site=_resolve_site)
app = FastAPI(title="RoverHub Backend (dev)", dependencies=[Depends(auth.guard)])
app.include_router(build_auth_router(auth))
app.include_router(build_mqtt_hooks_router(auth))
app.include_router(build_router(command_store, mqtt_client))
app.include_router(build_robots_router(registry))
app.include_router(build_media_router(clips_dir=os.environ.get("AUDIO_CLIPS_DIR", DEFAULT_AUDIO_CLIPS_DIR)))
# Camera snapshots travel the other way from audio clips -- edge to cloud. Unlike
# clips they are kept as a small media library (JPEG + JSON sidecar, listed by
# GET /snapshots) in a persistent directory. See control/snapshots.py.
def _site_for(robot_id: str):
    record = registry.get(robot_id)
    return record.site_id if record else None


app.include_router(build_snapshot_router(
    snapshots_dir=os.environ.get("SNAPSHOTS_DIR", DEFAULT_SNAPSHOTS_DIR),
    site_lookup=_site_for,
))
# Site maps: a surveyed floor plan packed by tools/map_prep/pack_map.py, kept as
# one directory per map. See maps/.
app.include_router(build_maps_router(maps_dir=os.environ.get("MAPS_DIR", DEFAULT_MAPS_DIR)))
_tasks_dir = os.environ.get("TASKS_DIR", DEFAULT_TASKS_DIR)
# Missions authored in the console, stored as one JSON file each so they
# survive a refresh and are shared between browsers. See missions/router.py.
# `tasks_dir` is what lets deleting a mission delete its tasks too.
app.include_router(build_missions_router(
    missions_dir=os.environ.get("MISSIONS_DIR", DEFAULT_MISSIONS_DIR),
    tasks_dir=_tasks_dir,
))
# Mission run records: one folder per run (run-records spec §6). See runs/.
# The copier shares the router's own RunStore so its per-run locks coordinate
# with PUT /runs; hence the forwarding hook, bound once the copier exists.
_copier = None
_run_ended = None  # bound below, once the task store exists
runs_router = build_runs_router(
    runs_dir=os.environ.get("RUNS_DIR", DEFAULT_RUNS_DIR),
    on_change=lambda run_id, path: _copier.on_change(run_id, path) if _copier else None,
    on_run_ended=lambda run: _run_ended(run) if _run_ended else None,
)
_copier = bucket_from_env(runs_router.store)
app.include_router(runs_router)
# Mission tasks (a mission assigned to a robot on a schedule) and the occurrence
# queue the scheduler materialises from them. See tasks/router.py.


def _scheduler_enabled() -> bool:
    # SCHEDULER_ENABLED=false (or 0/no/off) turns the background loop off: it
    # writes to TASKS_DIR every 30 s, which is not always wanted on a dev
    # machine. Default on. Read when asked, so it is a startup decision.
    return os.environ.get("SCHEDULER_ENABLED", "").strip().lower() not in ("0", "false", "no", "off")


# run-now is told whether the loop is on, so it does not promise a tick that
# will never come.
tasks_router = build_tasks_router(
    tasks_dir=_tasks_dir, scheduler_enabled=_scheduler_enabled,
    mission_store=MissionStore(os.environ.get("MISSIONS_DIR", DEFAULT_MISSIONS_DIR)),
)
app.include_router(tasks_router)


def _unattended_runs_enabled() -> bool:
    # Scheduled runs have nobody watching. They stay off until E-Stop has been
    # tested on a moving robot, so the default is off and only an explicit
    # true/1/yes/on turns them on. (SCHEDULER_ENABLED is the opposite: on unless
    # switched off.)
    return os.environ.get("UNATTENDED_RUNS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


_UNCOMPILED = "open and save this mission once, so its program is compiled for the robot"
_scheduler_missions = MissionStore(os.environ.get("MISSIONS_DIR", DEFAULT_MISSIONS_DIR))


def _publish_scheduled(occurrence: TaskOccurrence, run: MissionRun) -> None:
    """Send START_MISSION for a scheduled occurrence. Raising is how a refusal
    reaches the operator: the loop records it as the occurrence's failure."""
    mission = _scheduler_missions.get(occurrence.mission_id)
    robot = registry.get(run.robot_id)
    programs = mission.program_steps if mission is not None else None
    if programs is None or programs.compiler_version != PROGRAM_COMPILER_VERSION or robot is None:
        raise RuntimeError(_UNCOMPILED)
    if robot.robot_type == "quad":
        steps = programs.quad
    elif robot.robot_type == "wheel":
        steps = programs.wheel
    else:
        raise RuntimeError(_UNCOMPILED)
    if not steps:
        raise RuntimeError(_UNCOMPILED)
    if has_spoken_steps(steps):
        raise RuntimeError("scheduled runs cannot play speech yet")
    task = _reconcile_task_store.get_task(occurrence.task_id)
    minutes = task.max_duration_min if task is not None else None
    _run_dispatcher.start_scheduled(run, steps, minutes * 60 if minutes is not None else None)


# Its own MissionStore and TaskStore, not the routers': the directories are the
# source of truth and are re-read on every call, so a second instance over the
# same directory is coherent by construction. The run store is the router's own,
# so its per-run locks coordinate with PUT /runs.
scheduler_loop = SchedulerLoop(
    TaskStore(_tasks_dir), registry,
    MissionStore(os.environ.get("MISSIONS_DIR", DEFAULT_MISSIONS_DIR)),
    _publish_scheduled,
    run_store=runs_router.store, executor_kinds=EXECUTOR_KINDS,
    unattended_enabled=_unattended_runs_enabled(),
)
_reconcile_task_store = TaskStore(_tasks_dir)


_reconcile_hook = make_state_hook(
    runs_router.store, _reconcile_task_store,
    lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    # The copier is bound after the router, and may be absent: same as PUT /runs.
    on_change=lambda run_id, path: _copier.on_change(run_id, path) if _copier else None,
    registry=registry,
)
# Starting a run: POST /robots/{id}/runs. Same run store (and bucket hook) as
# PUT /runs, so its lock and copy behaviour match.
_run_dispatcher = RunDispatcher(
    registry, runs_router.store, _reconcile_task_store, command_store, mqtt_client.publish_command,
    on_change=lambda run_id, path: _copier.on_change(run_id, path) if _copier else None,
)
runs_router.dispatcher = _run_dispatcher
# Runs that never close: a START_MISSION nobody answered, or a run its bridge
# stopped naming. Checked every few seconds by the watchdog task below.
run_reaper = RunReaper(registry, runs_router.store, _run_dispatcher.start_failed)
_RUN_WATCH_INTERVAL_S = 5.0


def _sweep_runs() -> None:
    _run_dispatcher.sweep_unanswered()
    run_reaper.sweep()


async def _run_watchdog() -> None:
    while True:
        try:
            await asyncio.to_thread(_sweep_runs)
        except Exception:
            logger.exception("run watchdog sweep failed")
        await asyncio.sleep(_RUN_WATCH_INTERVAL_S)

tasks_router.dispatcher = _run_dispatcher
# The edge's run_ended closes the run record (runs/router.py); this settles the
# task occurrence that shares its id.
_run_ended = make_run_ended_hook(_reconcile_task_store)
# Rejects oversized audio-clip uploads by Content-Length before Starlette's
# multipart parser spools the body -- see ContentLengthLimitMiddleware's
# docstring in control/media.py for why this can't live in the handler alone.
app.add_middleware(ContentLengthLimitMiddleware, max_bytes=MAX_AUDIO_CLIP_BYTES)
# Without this, the browser's CORS preflight (an OPTIONS request, sent
# automatically because the frontend's fetch() calls carry a
# Content-Type: application/json body) gets a bare 405 from FastAPI's
# routing -- since no route ever declares an OPTIONS handler -- and the
# browser blocks the real POST/request from ever being sent. This isn't
# reproducible via FastAPI's TestClient (it calls the ASGI app directly and
# never enforces real browser CORS), which is why it went unnoticed in
# tests. No auth/cookies exist on this backend this round, so
# allow_credentials stays False and an explicit dev-origin allowlist is
# sufficient rather than needing wildcard-with-credentials gymnastics.
#
# FRONTEND_ORIGIN_REGEX additionally allows the console to be opened from
# another device on the LAN (`vite --host`) for demos. An exact allowlist cannot
# cover that: the dev machine's address is handed out by DHCP, so the origin
# changes without warning, and the failure mode is a silent one -- preflight
# 400, POST never sent, commands that appear to do nothing. The default below is
# restricted to RFC1918 private ranges, so it admits the office LAN but never a
# public origin. Tighten it to a fixed origin once auth exists.
_PRIVATE_LAN_ORIGIN_REGEX = (
    r"^http://(10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|[a-zA-Z0-9-]+\.local):\d+$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("FRONTEND_ORIGINS", "http://localhost:5173").split(","),
    allow_origin_regex=os.environ.get("FRONTEND_ORIGIN_REGEX", _PRIVATE_LAN_ORIGIN_REGEX),
    # The session cookie lets <img>/<video>/download links load signed-in.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _on_startup() -> None:
    mqtt_client.connect()
    app.state.run_watch_task = asyncio.create_task(_run_watchdog())
    if _scheduler_enabled():
        # Async so there is a running event loop to hang the task on. The tick
        # does its file I/O on a worker thread (SchedulerLoop.run), so it does
        # not stall requests.
        app.state.scheduler_task = asyncio.create_task(scheduler_loop.run())
        logger.info("scheduler loop started (set SCHEDULER_ENABLED=false to disable)")
    else:
        logger.info("scheduler loop disabled by SCHEDULER_ENABLED; no occurrences will be materialised")


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    task = getattr(app.state, "scheduler_task", None)
    watch = getattr(app.state, "run_watch_task", None)
    try:
        if watch is not None:
            watch.cancel()
            try:
                await watch
            except asyncio.CancelledError:
                pass
            app.state.run_watch_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # The loop already logs each tick's failure; one that escaped
                # it is worth a line, but must not skip the disconnect below.
                logger.exception("scheduler loop ended with an error")
            app.state.scheduler_task = None  # a later startup (or a test) must not see a stale one
    finally:
        mqtt_client.disconnect()


@app.get("/healthz")
def healthz():
    # server_time_ms: edge bridges stamp their messages from this clock rather
    # than their own, which can boot minutes wrong (robot_bridge/clock.py).
    return {"status": "ok", "server_time_ms": int(time.time() * 1000)}
