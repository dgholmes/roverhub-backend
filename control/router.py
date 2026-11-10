from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from auth.deps import acting_user_id, principal_of
from auth.policy import command_capabilities

from control.commands import CommandStore
from realtime.mqtt_client import CommandPublishError, UnknownRobotError
from shared.schemas.commands import (
    WIRED_COMMAND_TYPES, Command, CommandRequest, CommandStageEvent, NavGotoParams,
    ResetPositionParams, ResumeParams, command_ttl_s,
)


def build_router(command_store: CommandStore, mqtt_client, registry=None) -> APIRouter:
    """`registry` lets an unknown robot be refused before the command is even
    recorded. Without it the MQTT client still refuses to publish one
    (UnknownRobotError), so the answer is the same 404, just after the store."""
    router = APIRouter()

    @router.post("/robots/{robot_id}/commands")
    def issue_command(robot_id: str, request: CommandRequest, http: Request):
        if request.type not in WIRED_COMMAND_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"{request.type} is not wired to the real pipeline in this build",
            )

        if request.type == "START_MISSION":
            # A mission start needs its MissionRun and the dispatcher's checks;
            # sent from here the edge would post events to a run that is not there.
            raise HTTPException(
                status_code=409, detail=f"use POST /robots/{robot_id}/runs to start a mission")

        principal = principal_of(http)
        needed = command_capabilities(request.type)
        site_id = getattr(http.state, "site_id", None)
        if principal is not None and not any(principal.can(c, site_id) for c in needed):
            raise HTTPException(status_code=403, detail=f"your role does not allow {request.type}")

        # Known means registered, online or not: an offline robot is still sent
        # the command exactly as before. Unknown has no site, so no topic to send
        # it to; it used to go to roverhub/unknown/... and be reported as sent.
        if registry is not None and registry.get(robot_id) is None:
            raise HTTPException(status_code=404, detail="robot not found")

        # After the permission check, so a role that may not drive learns nothing
        # about what a valid goal or pose looks like.
        if request.type == "NAV_GOTO":
            try:
                NavGotoParams.model_validate(request.params)
            except ValidationError as exc:
                fields = ", ".join(".".join(str(p) for p in e["loc"]) or "params" for e in exc.errors())
                raise HTTPException(
                    status_code=422, detail=f"NAV_GOTO params are not a usable goal ({fields})") from exc
        elif request.type == "RESET_POSITION":
            try:
                ResetPositionParams.model_validate(request.params)
            except ValidationError as exc:
                fields = ", ".join(".".join(str(p) for p in e["loc"]) or "params" for e in exc.errors())
                raise HTTPException(
                    status_code=422, detail=f"RESET_POSITION params are not a usable pose ({fields})") from exc
        elif request.type == "RESUME":
            # The run_id is what stops a late Retry restarting some other paused run.
            try:
                ResumeParams.model_validate(request.params or {}, strict=True)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422, detail="RESUME needs the run_id of the run to retry") from exc

        command = Command(
            command_id=str(uuid.uuid4()),
            robot_id=robot_id,
            type=request.type,
            label=request.label,
            params=request.params,
            current_stage="requested",
            stages=[CommandStageEvent(stage="requested", at=datetime.now(timezone.utc).isoformat())],
            initiated_by=acting_user_id(http, request.initiated_by),
            # None for a stop: a late stop is still a stop. The bridge only logs
            # an expired command for now, so nothing may depend on it dropping one.
            ttl_s=command_ttl_s(request.type),
        )
        command_store.add(command)
        try:
            mqtt_client.publish_command(robot_id, command)
        except CommandPublishError as exc:
            # The command never reached the broker, so no bridge will ever ack it.
            # Record that terminally and fail the request rather than returning a
            # command that will sit at "Sending..." until the operator gives up.
            command.current_stage = "execution_failed"
            command.failure_reason = str(exc)
            command.stages.append(
                CommandStageEvent(
                    stage="execution_failed", at=datetime.now(timezone.utc).isoformat(),
                )
            )
            command_store.add(command)
            status = 404 if isinstance(exc, UnknownRobotError) else 503
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return command

    @router.get("/robots/{robot_id}/commands/{command_id}")
    def get_command(robot_id: str, command_id: str):
        command = command_store.get(command_id)
        if command is None or command.robot_id != robot_id:
            raise HTTPException(status_code=404, detail="command not found")
        return command

    return router
