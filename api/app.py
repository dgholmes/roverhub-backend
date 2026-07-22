from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI

from control.commands import CommandStore
from control.media import build_media_router
from control.router import build_router
from realtime.mqtt_client import BackendMqttClient, real_mqtt_client_factory
from robots.registry import RobotRegistry

load_dotenv()  # loads .env in the working directory, if present; real env vars still win

registry = RobotRegistry()
command_store = CommandStore()
mqtt_client = BackendMqttClient(
    os.environ.get("CLOUD_MQTT_URL", "mqtt://localhost:1883"), registry, real_mqtt_client_factory,
    on_ack=command_store.apply_ack,
)

app = FastAPI(title="RoverHub Backend (dev)")
app.include_router(build_router(command_store, mqtt_client))
app.include_router(build_media_router(clips_dir=os.environ.get("AUDIO_CLIPS_DIR", "/tmp/roverhub-audio-clips")))


@app.on_event("startup")
def _on_startup() -> None:
    mqtt_client.connect()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    mqtt_client.disconnect()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/robots")
def list_robots():
    return [
        {
            "robot_id": r.robot_id,
            "site_id": r.site_id,
            "robot_type": r.robot_type,
            "name": r.name,
            "online": r.online,
            "abstract_state": r.abstract_state,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        }
        for r in registry.list_all()
    ]
