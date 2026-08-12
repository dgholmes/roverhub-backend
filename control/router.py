from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from control.commands import CommandStore
from realtime.mqtt_client import CommandPublishError
from shared.schemas.commands import Command, CommandRequest, CommandStageEvent, WIRED_COMMAND_TYPES


def build_router(command_store: CommandStore, mqtt_client) -> APIRouter:
    router = APIRouter()

    @router.post("/robots/{robot_id}/commands")
    def issue_command(robot_id: str, request: CommandRequest):
        if request.type not in WIRED_COMMAND_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"{request.type} is not wired to the real pipeline in this build",
            )

        command = Command(
            command_id=str(uuid.uuid4()),
            robot_id=robot_id,
            type=request.type,
            label=request.label,
            params=request.params,
            current_stage="requested",
            stages=[CommandStageEvent(stage="requested", at=datetime.now(timezone.utc).isoformat())],
            initiated_by=request.initiated_by,
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
            raise HTTPException(status_code=503, detail=str(exc))
        return command

    @router.get("/robots/{robot_id}/commands/{command_id}")
    def get_command(robot_id: str, command_id: str):
        command = command_store.get(command_id)
        if command is None or command.robot_id != robot_id:
            raise HTTPException(status_code=404, detail="command not found")
        return command

    return router
