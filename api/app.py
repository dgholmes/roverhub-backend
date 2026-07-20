from __future__ import annotations

from fastapi import FastAPI

from robots.registry import RobotRegistry

registry = RobotRegistry()

app = FastAPI(title="RoverHub Backend (dev)")


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
