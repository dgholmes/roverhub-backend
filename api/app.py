from __future__ import annotations

import os

from fastapi import FastAPI

from realtime.mqtt_client import BackendMqttClient, real_mqtt_client_factory
from robots.registry import RobotRegistry

registry = RobotRegistry()
mqtt_client = BackendMqttClient(
    os.environ.get("CLOUD_MQTT_URL", "mqtt://localhost:1883"), registry, real_mqtt_client_factory,
)

app = FastAPI(title="RoverHub Backend (dev)")


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
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        }
        for r in registry.list_all()
    ]
