"""History's API models. Backend-only for now: not in shared/schemas, so the
console's types for these are hand-written until the shape settles."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

IncidentWorkflowState = Literal["open", "acknowledged", "resolved"]
IncidentOutcome = Literal["confirmed", "false_alarm", "other"]


class Evidence(BaseModel):
    snapshot_id: str
    url: str


class IncidentOut(BaseModel):
    incident_id: str
    robot_id: str
    site_id: str
    rule_id: Optional[str] = None
    label: str
    severity: str
    opened_at: str
    camera: Optional[str] = None
    message: Optional[str] = None
    confidence: Optional[float] = None
    x_m: Optional[float] = None
    y_m: Optional[float] = None
    map_id: Optional[str] = None
    run_id: Optional[str] = None
    received_at: str
    last_seen: str
    state: IncidentWorkflowState
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    outcome: Optional[IncidentOutcome] = None
    note: Optional[str] = None
    evidence: List[Evidence] = Field(default_factory=list)
    # The Incident exactly as perception published it.
    payload: Dict[str, Any]


class IncidentPage(BaseModel):
    items: List[IncidentOut]
    next_cursor: Optional[str] = None


class ResolveBody(BaseModel):
    outcome: IncidentOutcome
    note: Optional[str] = Field(None, max_length=2000)


class Sample(BaseModel):
    at: str
    battery_pct: Optional[float] = None
    x_m: Optional[float] = None
    y_m: Optional[float] = None
    heading_deg: Optional[float] = None
    position_quality: Optional[str] = None
    speed_mps: Optional[float] = None
    abstract_state: Optional[str] = None
    sdk_state: Optional[str] = None
    posture: Optional[str] = None
    control_owner: Optional[str] = None
    active_run_id: Optional[str] = None
    link: Optional[str] = None
    reason: str


class RobotHistory(BaseModel):
    robot_id: str
    from_: str = Field(alias="from")
    to: str
    samples: List[Sample]

    model_config = {"populate_by_name": True}


class AuditEntry(BaseModel):
    id: int
    at: str
    actor: str
    action: str
    target: Optional[str] = None
    outcome: Optional[str] = None
    site_id: Optional[str] = None
    robot_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class AuditPage(BaseModel):
    items: List[AuditEntry]
    next_cursor: Optional[str] = None


class HistoryStatus(BaseModel):
    available: bool
    engine: Optional[str] = None
    migration: Optional[int] = None
    rows: Dict[str, int] = Field(default_factory=dict)
    queue_depth: int = 0
    dropped: Dict[str, int] = Field(default_factory=dict)
    written: int = 0
    write_errors: int = 0
    connected: Optional[bool] = None
    last_error: Optional[str] = None
    last_write_at: Optional[str] = None
