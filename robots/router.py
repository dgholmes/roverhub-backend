"""Robot registry endpoints: listing, and the informational acceptance flow.

Acceptance does NOT gate anything -- not commands, not telemetry, not E-Stop.
See the comment block at the top of robots/registry.py for why, and for what
would have to exist (auth, a durable store) before it could.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from robots.registry import RobotRecord, RobotRegistry


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def robot_json(record: RobotRecord) -> dict:
    return {
        "robot_id": record.robot_id,
        "site_id": record.site_id,
        "robot_type": record.robot_type,
        "name": record.name,
        "online": record.online,
        "abstract_state": record.abstract_state,
        "last_seen": _iso(record.last_seen),
        "first_seen": _iso(record.first_seen),
        "bridge_id": record.bridge_id,
        "schema_version": record.schema_version,
        "acceptance": record.acceptance,
        "acceptance_source": record.acceptance_source,
        "acceptance_changed_at": _iso(record.acceptance_changed_at),
        "host": record.host,
        "host_metrics": record.host_metrics,
        "host_metrics_at": record.host_metrics_at,
        "nav_goal": record.nav_goal,
    }


def build_robots_router(registry: RobotRegistry) -> APIRouter:
    router = APIRouter()

    @router.get("/robots")
    def list_robots(acceptance: Optional[str] = Query(None, pattern="^(accepted|pending|rejected)$")):
        return [
            robot_json(r) for r in registry.list_all()
            if acceptance is None or r.acceptance == acceptance
        ]

    # Declared before any /robots/{robot_id} GET could shadow it.
    @router.get("/robots/pending")
    def list_pending():
        """Devices waiting to be accepted. Always empty in the default
        DEVICE_ACCEPTANCE=auto mode, where every bridge is accepted on sight."""
        return [robot_json(r) for r in registry.list_all() if r.acceptance == "pending"]

    def _decide(robot_id: str, acceptance: str) -> dict:
        record = registry.set_acceptance(robot_id, acceptance)
        if record is None:
            raise HTTPException(status_code=404, detail="robot not found")
        return robot_json(record)

    @router.post("/robots/{robot_id}/accept")
    def accept(robot_id: str):
        return _decide(robot_id, "accepted")

    @router.post("/robots/{robot_id}/reject")
    def reject(robot_id: str):
        """Marks the device rejected. Informational: a rejected robot's bridge
        keeps reporting and remains commandable, because nothing here can
        authenticate it and nothing may cut off a robot over a flag."""
        return _decide(robot_id, "rejected")

    @router.delete("/robots/{robot_id}")
    def forget(robot_id: str):
        """Drops the record. A bridge still running re-registers within one
        heartbeat (~5s) and reappears as new."""
        if not registry.forget(robot_id):
            raise HTTPException(status_code=404, detail="robot not found")
        return {"robot_id": robot_id, "forgotten": True}

    return router
