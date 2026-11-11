"""History routes: incidents and their workflow, a robot's history, the audit
log, and the store's own status. Access rules are in auth/policy.py.

    GET  /incidents?site_id&robot_id&state&kind&from&to&limit&cursor   newest first, the caller's sites
    GET  /incidents/{incident_id}                                      with evidence URLs
    POST /incidents/{incident_id}/acknowledge
    POST /incidents/{incident_id}/resolve        {outcome, note}
    GET  /robots/{robot_id}/history?from&to&max_points               thinned on the server
    GET  /audit?site_id&robot_id&actor&action&target&from&to&limit&cursor
         newest first; `action=map.*` matches a family; only the rows the
         caller may read (auth/deps.py audit_scope)
    GET  /history/status

Every route answers 503, with the reason, when history could not start.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from auth.deps import DEV_USER_ID, audit_scope, principal_of, visible_sites
from history.db import epoch, iso
from history.models import (
    AuditPage, HistoryStatus, IncidentOut, IncidentPage, IncidentWorkflowState, ResolveBody, RobotHistory,
)
from history.service import History
from history.store import IncidentConflict, IncidentNotFound

DEFAULT_WINDOW_S = 24 * 3600.0
MAX_POINTS_CAP = 5000


def _actor(request: Request) -> str:
    """The signed-in principal; `operator-dev` when sign-in is off. Never the body."""
    principal = principal_of(request)
    return principal.id if principal is not None else DEV_USER_ID


def build_history_router(
    history: History,
    evidence_for: Callable[[str], List[str]] = lambda incident_id: [],
    robot_known: Callable[[str], bool] = lambda robot_id: False,
    clock: Callable[[], float] = time.time,
) -> APIRouter:
    """`evidence_for(incident_id)`: snapshot ids whose upload named the incident
    (control/snapshots.py). `robot_known(robot_id)`: the registry knows it."""
    router = APIRouter()

    def _store():
        if not history.available:
            raise HTTPException(status_code=503, detail=(
                f"history is not available on this backend ({history.error}); see the backend log"))
        return history.store

    def _with_evidence(incident: dict) -> dict:
        ids = list(incident["evidence_snapshot_ids"])
        try:
            linked = evidence_for(incident["incident_id"])
        except Exception:
            linked = []
        ids += [s for s in linked if s not in ids]
        return {**incident, "evidence": [{"snapshot_id": s, "url": f"/snapshots/{s}"} for s in ids]}

    def _page_args(fn, **kwargs):
        try:
            return fn(**kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @router.get("/incidents", response_model=IncidentPage)
    def list_incidents(
        request: Request,
        site_id: Optional[str] = None, robot_id: Optional[str] = None,
        state: Optional[IncidentWorkflowState] = None, kind: Optional[str] = None,
        from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
        limit: int = Query(50, ge=1, le=500), cursor: Optional[str] = None,
    ):
        items, next_cursor = _page_args(
            _store().list_incidents, site_id=site_id, robot_id=robot_id, state=state, kind=kind,
            since=from_, until=to, limit=limit, cursor=cursor, sites=visible_sites(request))
        return {"items": [_with_evidence(i) for i in items], "next_cursor": next_cursor}

    @router.get("/incidents/{incident_id}", response_model=IncidentOut)
    def get_incident(incident_id: str):
        incident = _store().get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return _with_evidence(incident)

    @router.post("/incidents/{incident_id}/acknowledge", response_model=IncidentOut)
    def acknowledge(incident_id: str, request: Request):
        store = _store()
        try:
            return _with_evidence(store.acknowledge(incident_id, _actor(request), clock()))
        except IncidentNotFound:
            raise HTTPException(status_code=404, detail="incident not found") from None
        except IncidentConflict as exc:
            raise HTTPException(status_code=409, detail=exc.message) from None

    @router.post("/incidents/{incident_id}/resolve", response_model=IncidentOut)
    def resolve(incident_id: str, body: ResolveBody, request: Request):
        store = _store()
        try:
            return _with_evidence(store.resolve(incident_id, _actor(request), body.outcome, body.note, clock()))
        except IncidentNotFound:
            raise HTTPException(status_code=404, detail="incident not found") from None
        except IncidentConflict as exc:
            raise HTTPException(status_code=409, detail=exc.message) from None

    @router.get("/robots/{robot_id}/history", response_model=RobotHistory, response_model_by_alias=True)
    def robot_history(
        robot_id: str, request: Request, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
        max_points: int = Query(500, ge=1, le=MAX_POINTS_CAP),
    ):
        # The guard has checked watchLive at the robot's site (the registry's,
        # or its newest sample's when the registry does not know it: api/app.py
        # _robot_site). Samples from sites the caller cannot see are left out
        # as well, for a robot that has moved sites.
        sites = visible_sites(request)
        store = _store()
        until = epoch(to) if to is not None else clock()
        since = epoch(from_) if from_ is not None else (until - DEFAULT_WINDOW_S if until is not None else None)
        if until is None or since is None:
            raise HTTPException(status_code=400, detail="from and to must be ISO 8601 timestamps")
        if since >= until:
            raise HTTPException(status_code=400, detail="from must be before to")
        if not robot_known(robot_id) and not store.has_samples(robot_id, sites):
            raise HTTPException(status_code=404, detail="robot not found")
        return {"robot_id": robot_id, "from": iso(since), "to": iso(until),
                "samples": store.history(robot_id, since, until, max_points, sites=sites)}

    @router.get("/audit", response_model=AuditPage)
    def list_audit(
        request: Request,
        site_id: Optional[str] = None, robot_id: Optional[str] = None,
        actor: Optional[str] = None, action: Optional[str] = None, target: Optional[str] = None,
        from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
        limit: int = Query(50, ge=1, le=500), cursor: Optional[str] = None,
    ):
        # `actor` is the user id (or robot id, `operator-dev`, `system`,
        # `schedule`) the server attributed the row to.
        items, next_cursor = _page_args(
            _store().list_audit, actor=actor, action=action, target=target, since=from_, until=to,
            limit=limit, cursor=cursor, site_id=site_id, robot_id=robot_id,
            scope=audit_scope(principal_of(request)))
        return {"items": items, "next_cursor": next_cursor}

    @router.get("/history/status", response_model=HistoryStatus)
    def status():
        _store()
        out = history.status()
        return {**out, "last_write_at": iso(out["last_write_at"])}

    return router
