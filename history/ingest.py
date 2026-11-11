"""What an MQTT message becomes in the history. Runs on paho's thread, so it is
in-memory arithmetic only: it decides whether a row is due and builds it; the
writer thread does the database work.

Telemetry is decimated per robot: a row on the first message, then one per
`interval_s`, plus a row at once when something an operator would want to see
on a timeline changes -- abstract state (E-Stop included), posture, control
owner, the bridge's link freshness, or the battery crossing a 5 % step. The row
merges the latest /telemetry and /state for the robot. A new active run alone is
recorded on the next row rather than forcing one: the run's own record is in
runs/.

Times are this server's clock, not the device's: one clock for every robot, so
retention and the history query's windows mean the same thing for all of them.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Dict, Optional

from pydantic import ValidationError

from history.records import IncidentRecord, PerceptionRecord, SampleRecord
from shared.schemas.detection import Incident

logger = logging.getLogger("roverhub.history")

BATTERY_STEP_PCT = 5.0


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _str(value) -> Optional[str]:
    return value if isinstance(value, str) else None


class TelemetryDecimator:
    def __init__(self, interval_s: float = 5.0, clock: Callable[[], float] = time.time):
        self.interval_s = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._robots: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _bucket(battery: Optional[float]):
        return None if battery is None else math.floor(battery / BATTERY_STEP_PCT)

    @staticmethod
    def _triggers(v: Dict[str, Any]) -> tuple:
        return (v.get("abstract_state"), v.get("posture"), v.get("control_owner"), v.get("link"),
                TelemetryDecimator._bucket(v.get("battery_pct")))

    def on_telemetry(self, site_id: str, robot_id: str, data: Dict[str, Any]) -> Optional[SampleRecord]:
        vel = data.get("vel_body")
        speed = None
        if isinstance(vel, (list, tuple)) and len(vel) >= 2:
            vx, vy = _num(vel[0]), _num(vel[1])
            if vx is not None and vy is not None:
                speed = math.hypot(vx, vy)
        position = data.get("position") if isinstance(data.get("position"), dict) else {}
        return self._update(site_id, robot_id, {
            "battery_pct": _num(data.get("battery_percent")),
            "speed_mps": speed,
            # The bridge's map position (0.0.15+); an older bridge sends none.
            "x_m": _num(position.get("x_m")), "y_m": _num(position.get("y_m")),
            "heading_deg": _num(position.get("heading_deg")),
            "position_quality": _str(position.get("quality")),
            "link": _str(data.get("grpc_freshness")),
        })

    def on_state(self, site_id: str, robot_id: str, data: Dict[str, Any]) -> Optional[SampleRecord]:
        run = data.get("active_run") if isinstance(data.get("active_run"), dict) else {}
        return self._update(site_id, robot_id, {
            "abstract_state": _str(data.get("abstract_state")), "sdk_state": _str(data.get("sdk_state")),
            "posture": _str(data.get("posture")), "control_owner": _str(data.get("control_owner")),
            "active_run_id": _str(run.get("run_id")),
        })

    def _update(self, site_id: str, robot_id: str, values: Dict[str, Any]) -> Optional[SampleRecord]:
        now = self._clock()
        with self._lock:
            entry = self._robots.get(robot_id)
            if entry is None:
                entry = self._robots[robot_id] = {"values": {}, "last_row": None}
            before = self._triggers(entry["values"])
            entry["values"].update(values)
            entry["site_id"] = site_id
            last = entry["last_row"]
            if last is None:
                reason = "first"
            elif self._triggers(entry["values"]) != before:
                reason = "change"
            elif now - last >= self.interval_s:
                reason = "interval"
            else:
                return None
            entry["last_row"] = now
            v = dict(entry["values"])
        return SampleRecord(
            robot_id=robot_id, site_id=site_id, at=now, battery_pct=v.get("battery_pct"),
            x_m=v.get("x_m"), y_m=v.get("y_m"), heading_deg=v.get("heading_deg"),
            position_quality=v.get("position_quality"), speed_mps=v.get("speed_mps"),
            abstract_state=v.get("abstract_state"), sdk_state=v.get("sdk_state"), posture=v.get("posture"),
            control_owner=v.get("control_owner"), active_run_id=v.get("active_run_id"), link=v.get("link"),
            reason=reason)


class PerceptionTracker:
    """A row when a robot's perception status changes: the source state, or a
    detector's status. fps moves on every message and is not a change."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._last: Dict[str, tuple] = {}

    def on_status(self, site_id: str, robot_id: str, data: Dict[str, Any]) -> Optional[PerceptionRecord]:
        detectors = [d for d in (data.get("detectors") or []) if isinstance(d, dict)]
        key = (_str(data.get("source_state")), _str(data.get("camera")),
               tuple(sorted((str(d.get("detector_id")), str(d.get("status"))) for d in detectors)))
        with self._lock:
            if self._last.get(robot_id) == key:
                return None
            self._last[robot_id] = key
        return PerceptionRecord(robot_id=robot_id, site_id=site_id, at=self._clock(), source_state=key[0],
                                fps=_num(data.get("fps")), camera=key[1], detectors=detectors, payload=dict(data))


def incident_record(site_id: str, robot_id: str, data: Dict[str, Any], now: float) -> Optional[IncidentRecord]:
    """An incident message as a record, or None if it is not an Incident. Robot
    and site come from the topic -- the broker binds a device to its own topics,
    while the payload says whatever the sender wrote. The payload is kept as
    sent, extra fields included (a map position or run id from a newer service)."""
    try:
        Incident.model_validate(data)
    except ValidationError as exc:
        logger.warning("dropping an incident from %s that is not an Incident: %s", robot_id, exc)
        return None
    if data.get("robot_id") != robot_id or data.get("site_id") != site_id:
        logger.warning("incident %s names %s/%s but came on %s/%s's topic; using the topic",
                       data.get("incident_id"), data.get("site_id"), data.get("robot_id"), site_id, robot_id)
    return IncidentRecord(payload=dict(data), robot_id=robot_id, site_id=site_id, received_at=now)
