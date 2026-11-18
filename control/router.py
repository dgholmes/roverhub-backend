from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from auth.deps import acting_user_id, principal_of
from auth.policy import command_capabilities

from control.commands import CommandStore
from realtime.mqtt_client import CommandPublishError, UnknownRobotError
from realtime.mqtt_client import _parse_version
from shared.schemas.commands import (
    WIRED_COMMAND_TYPES, Command, CommandRequest, CommandStageEvent, NavGotoParams, NavResolveParams,
    ResetPositionParams, ResumeParams, SetNavStuckPolicyParams, SyncMapParams, command_ttl_s,
)
from shared.schemas.sensors import SetCalibrationModeParams, SetLidarMountParams
from shared.schemas.survey import FindMovingParams, StartSurveyParams, StopSurveyParams

# Surveying and lidar calibration (schema 0.0.23). A bridge older than that
# does not know these types; refused here with the reason rather than sent to
# sit at "Sending..." (the console also offers them only from 0.0.23).
SURVEY_SCHEMA_VERSION = "0.0.23"
_SITE_SETUP_PARAMS = {
    "START_SURVEY": StartSurveyParams,
    "STOP_SURVEY": StopSurveyParams,
    "FIND_MOVING": FindMovingParams,
    "SET_LIDAR_MOUNT": SetLidarMountParams,
    "SET_CALIBRATION_MODE": SetCalibrationModeParams,
}
# What each one's params are, in the 422's words.
_SITE_SETUP_NOUN = {
    "START_SURVEY": "a survey to start",
    "STOP_SURVEY": "a survey to stop",
    "FIND_MOVING": "a moving-object search",
    "SET_LIDAR_MOUNT": "a lidar mount",
    "SET_CALIBRATION_MODE": "a calibration mode (off, suggest or auto)",
}
# STOP_SURVEY is a stop: never refused on a version guess (an old bridge has no
# survey to stop, and says so itself).
_NEEDS_SURVEY_SCHEMA = frozenset(_SITE_SETUP_PARAMS) - {"STOP_SURVEY"}

# "Send to robot" (schema 0.0.26): SYNC_MAP makes the robot's map files the
# floor's current version. A bridge older than that refuses the type as unknown.
SYNC_MAP_SCHEMA_VERSION = "0.0.26"


# A stuck walk (schema 0.0.27): NAV_RESOLVE answers it, SET_NAV_STUCK_POLICY
# is the robot's setting. A bridge older than that refuses both as unknown.
NAV_STUCK_SCHEMA_VERSION = "0.0.27"
_NAV_STUCK_PARAMS = {
    "NAV_RESOLVE": (NavResolveParams, "an answer to a stuck walk"),
    "SET_NAV_STUCK_POLICY": (SetNavStuckPolicyParams, "a stuck policy (ask, auto or stop)"),
}


def _bad_fields(exc: ValidationError) -> str:
    return ", ".join(".".join(str(p) for p in e["loc"]) or "params" for e in exc.errors())


def build_router(command_store: CommandStore, mqtt_client, registry=None, map_lookup=None) -> APIRouter:
    """`registry` lets an unknown robot be refused before the command is even
    recorded. Without it the MQTT client still refuses to publish one
    (UnknownRobotError), so the answer is the same 404, just after the store.
    `map_lookup(map_id)` -> SiteMap or None: the map a START_SURVEY (re-survey)
    or FIND_MOVING names must be at the robot's site. Without it that is not
    checked here (the robot's own upload is still held to its site)."""
    router = APIRouter()

    def _check_site_setup(robot_id: str, command_type: str, params) -> None:
        """422 / 404 / 409 for a survey or calibration command that cannot work."""
        try:
            parsed = _SITE_SETUP_PARAMS[command_type].model_validate(params or {})
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"{command_type} params are not {_SITE_SETUP_NOUN[command_type]} ({_bad_fields(exc)})",
            ) from exc
        robot = registry.get(robot_id) if registry is not None else None
        if robot is not None and command_type in _NEEDS_SURVEY_SCHEMA:
            version = _parse_version(robot.schema_version)
            if version is None or version < _parse_version(SURVEY_SCHEMA_VERSION):
                raise HTTPException(
                    status_code=409,
                    detail=(f"update the edge device: it runs schema {robot.schema_version or 'unknown'}, "
                            f"surveys and lidar calibration need {SURVEY_SCHEMA_VERSION}"))
        map_id = getattr(parsed, "map_id", None)
        if map_id is None or map_lookup is None:
            return
        site_map = map_lookup(map_id)
        robot_site = robot.site_id if robot is not None else None
        # A map at another site reads as not found: the robot's key could not
        # upload to it, and this caller's role here says nothing about there.
        if site_map is None or (robot_site is not None and site_map.site_id != robot_site):
            raise HTTPException(status_code=404, detail=f"map {map_id} not found at this robot's site")
        if command_type == "FIND_MOVING" and not any(f.index == parsed.floor_index for f in site_map.floors):
            raise HTTPException(status_code=404, detail=f"map {map_id} has no floor {parsed.floor_index}")

    def _check_sync_map(robot_id: str, params) -> None:
        """422 / 409 / 404 for a SYNC_MAP the robot would refuse anyway, said
        here in plain words before anything is sent. The robot checks all of
        it again: the hash of the files it builds is the last word."""
        try:
            parsed = SyncMapParams.model_validate(params or {})
        except ValidationError as exc:
            raise HTTPException(status_code=422,
                                detail=f"SYNC_MAP params are not a map to send ({_bad_fields(exc)})") from exc
        robot = registry.get(robot_id) if registry is not None else None
        if robot is not None:
            version = _parse_version(robot.schema_version)
            if version is None or version < _parse_version(SYNC_MAP_SCHEMA_VERSION):
                raise HTTPException(
                    status_code=409,
                    detail=(f"update the edge device: it runs schema {robot.schema_version or 'unknown'}, "
                            f"sending it a map needs {SYNC_MAP_SCHEMA_VERSION}"))
            if robot.nav_map_id is not None and robot.nav_map_id != parsed.map_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"{robot_id} is set up for map {robot.nav_map_id}, not {parsed.map_id}")
        if map_lookup is None:
            return
        site_map = map_lookup(parsed.map_id)
        robot_site = robot.site_id if robot is not None else None
        if site_map is None or (robot_site is not None and site_map.site_id != robot_site):
            raise HTTPException(status_code=404, detail=f"map {parsed.map_id} not found at this robot's site")
        floor = next((f for f in site_map.floors if f.index == parsed.floor_index), None)
        if floor is None:
            raise HTTPException(status_code=404, detail=f"map {parsed.map_id} has no floor {parsed.floor_index}")
        if floor.content_hash is None:
            raise HTTPException(
                status_code=409,
                detail=f"map {parsed.map_id} floor {parsed.floor_index} has no content hash, so the robot "
                       "could not check what it receives")
        if floor.content_hash != parsed.map_hash:
            raise HTTPException(
                status_code=409,
                detail=f"map {parsed.map_id} floor {parsed.floor_index} is now version "
                       f"{floor.content_hash[:12]}, not {parsed.map_hash[:12]} -- reload the map and send it again")

    def _check_nav_stuck(robot_id: str, command_type: str, params) -> None:
        """422 / 409 for a NAV_RESOLVE or SET_NAV_STUCK_POLICY the robot would
        refuse anyway. Whether the walk is stuck, and the option offered, are
        the robot's to judge."""
        model, noun = _NAV_STUCK_PARAMS[command_type]
        try:
            model.model_validate(params or {})
        except ValidationError as exc:
            raise HTTPException(status_code=422,
                                detail=f"{command_type} params are not {noun} ({_bad_fields(exc)})") from exc
        robot = registry.get(robot_id) if registry is not None else None
        if robot is not None:
            version = _parse_version(robot.schema_version)
            if version is None or version < _parse_version(NAV_STUCK_SCHEMA_VERSION):
                raise HTTPException(
                    status_code=409,
                    detail=(f"update the edge device: it runs schema {robot.schema_version or 'unknown'}, "
                            f"stuck walks need {NAV_STUCK_SCHEMA_VERSION}"))

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
                raise HTTPException(
                    status_code=422, detail=f"NAV_GOTO params are not a usable goal ({_bad_fields(exc)})") from exc
        elif request.type == "RESET_POSITION":
            try:
                ResetPositionParams.model_validate(request.params)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"RESET_POSITION params are not a usable pose ({_bad_fields(exc)})") from exc
        elif request.type in _SITE_SETUP_PARAMS:
            _check_site_setup(robot_id, request.type, request.params)
        elif request.type == "SYNC_MAP":
            _check_sync_map(robot_id, request.params)
        elif request.type in _NAV_STUCK_PARAMS:
            _check_nav_stuck(robot_id, request.type, request.params)
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
            # How stale the bridge may find it before refusing it
            # REJECTED_EXPIRED (by type, and a Release & resume by its params).
            # None for a stop: a late stop is still a stop. The bridge enforces
            # it only once its clock is synced with ours (robot_bridge/clock.py).
            ttl_s=command_ttl_s(request.type, request.params),
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
