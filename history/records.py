"""What the writer queues: one record per row to write. `kind` names the
counter a dropped one is counted under; `vital` ones (incidents, audit) are the
last to be dropped when the queue is full."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class IncidentRecord:
    """An Incident as published, with robot and site from the MQTT topic."""
    payload: Dict[str, Any]
    robot_id: str
    site_id: str
    received_at: float
    kind = "incident"
    vital = True


@dataclass(frozen=True)
class SampleRecord:
    robot_id: str
    site_id: Optional[str]
    at: float
    # None means unknown -- never 0.
    battery_pct: Optional[float]
    x_m: Optional[float]
    y_m: Optional[float]
    heading_deg: Optional[float]
    position_quality: Optional[str]
    speed_mps: Optional[float]
    abstract_state: Optional[str]
    sdk_state: Optional[str]
    posture: Optional[str]
    control_owner: Optional[str]
    active_run_id: Optional[str]
    # The bridge's gRPC freshness ("live" | "stale" | "missing") as last reported.
    link: Optional[str]
    # Why this row was written: "first", "interval" or "change".
    reason: str
    kind = "telemetry"
    vital = False


@dataclass(frozen=True)
class PerceptionRecord:
    robot_id: str
    site_id: Optional[str]
    at: float
    source_state: Optional[str]
    fps: Optional[float]
    camera: Optional[str]
    detectors: List[Dict[str, Any]] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    kind = "perception"
    vital = False


@dataclass(frozen=True)
class AuditRecord:
    at: float
    actor: str
    action: str
    target: Optional[str]
    outcome: Optional[str]
    detail: Dict[str, Any] = field(default_factory=dict)
    site_id: Optional[str] = None
    # The robot the action concerns, when there is one (migration 0002).
    robot_id: Optional[str] = None
    kind = "audit"
    vital = True


RECORD_TYPES = (IncidentRecord, SampleRecord, PerceptionRecord, AuditRecord)
