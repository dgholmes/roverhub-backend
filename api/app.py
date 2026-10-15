from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from control.commands import CommandStore
from control.media import MAX_AUDIO_CLIP_BYTES, ContentLengthLimitMiddleware, build_media_router
from control.snapshots import DEFAULT_SNAPSHOTS_DIR, build_snapshot_router
from control.router import build_router
from realtime.mqtt_client import BackendMqttClient, real_mqtt_client_factory
from robots.registry import RobotRegistry
from robots.router import build_robots_router

load_dotenv()  # loads .env in the working directory, if present; real env vars still win

# Device acceptance is informational and defaults to "auto" -- every bridge
# that registers is accepted, as before. See robots/registry.py for why it is
# not (and cannot yet be) a gate. DEVICE_ACCEPTANCE=manual makes unknown
# devices arrive "pending"; ACCEPTED_DEVICES pre-accepts robot or bridge ids.
registry = RobotRegistry(
    acceptance_mode=os.environ.get("DEVICE_ACCEPTANCE", "auto").strip().lower() or "auto",
    preaccepted=os.environ.get("ACCEPTED_DEVICES", "").split(","),
)
command_store = CommandStore()
mqtt_client = BackendMqttClient(
    os.environ.get("CLOUD_MQTT_URL", "mqtt://localhost:1883"), registry, real_mqtt_client_factory,
    on_ack=command_store.apply_ack,
)

app = FastAPI(title="RoverHub Backend (dev)")
app.include_router(build_router(command_store, mqtt_client))
app.include_router(build_robots_router(registry))
app.include_router(build_media_router(clips_dir=os.environ.get("AUDIO_CLIPS_DIR", "/tmp/roverhub-audio-clips")))
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
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup() -> None:
    mqtt_client.connect()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    mqtt_client.disconnect()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
