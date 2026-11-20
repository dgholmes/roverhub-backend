from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from auth.deps import acting_user_id, principal_of, require_site_capability, site_visible
from auth.policy import command_capabilities

from control.commands import CommandStore
from realtime.mqtt_client import CommandPublishError, UnknownRobotError
from realtime.mqtt_client import _parse_version
from shared.schemas.commands import (
    WIRED_COMMAND_TYPES, Command, CommandRequest, CommandStageEvent, NavGotoParams, NavResolveParams,
    ResetPositionParams, ResumeParams, SetNavStuckPolicyParams, SetSiteParams, SyncMapParams, UseNavMapParams,
    command_ttl_s,
)
from shared.schemas.sensors import SetCalibrationModeParams, SetLidarMountParams
from shared.schemas.survey import (
    FindMovingParams, ReprocessSurveyParams, SetSurveyKeepParams, StartSurveyParams, StopSurveyParams,
    SurveyIdParams,
)

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

# The map ready checklist (schema 0.0.29). A bridge older than that refuses
# each as unknown. Per type: its params, what they are in the 422's words, and
# what needs the version in the 409's.
MAP_READY_SCHEMA_VERSION = "0.0.29"
_MAP_READY_PARAMS = {
    "REPROCESS_SURVEY": (ReprocessSurveyParams, "a survey to process again", "processing a survey again"),
    "SET_SURVEY_KEEP": (SetSurveyKeepParams, "a recording to keep or not", "keeping a recording"),
    "DELETE_SURVEY_RECORDING": (SurveyIdParams, "a recording to delete", "deleting a recording"),
    "SKIP_MOVING_SEARCH": (SurveyIdParams, "a moving-object search to skip",
                           "skipping the search for things that moved"),
    "USE_NAV_MAP": (UseNavMapParams, "a map to use", "using a map"),
    "SET_SITE": (SetSiteParams, "a site to move to", "moving a robot to another site"),
}


def _bad_fields(exc: ValidationError) -> str:
    return ", ".join(".".join(str(p) for p in e["loc"]) or "params" for e in exc.errors())


def build_router(command_store: CommandStore, mqtt_client, registry=None, map_lookup=None,
                 site_known=None) -> APIRouter:
    """`registry` lets an unknown robot be refused before the command is even
    recorded. Without it the MQTT client still refuses to publish one
    (UnknownRobotError), so the answer is the same 404, just after the store.
    `map_lookup(map_id)` -> SiteMap or None: the map a START_SURVEY (re-survey),
    REPROCESS_SURVEY, FIND_MOVING or USE_NAV_MAP names. Without it that is not
    checked here (the robot's own upload is still held to its site).
    `site_known(site_id)` -> bool: whether a SET_SITE names a site the backend
    knows. Without it any well-formed site id is sent."""
    router = APIRouter()

    def _map_at_robot_site(robot, map_id: str):
        """The map, or 404. A map at another site reads as not found: the
        robot's key could not upload to it, and this caller's role here says
        nothing about there."""
        site_map = map_lookup(map_id)
        robot_site = robot.site_id if robot is not None else None
        if site_map is None or (robot_site is not None and site_map.site_id != robot_site):
            raise HTTPException(status_code=404, detail=f"map {map_id} not found at this robot's site")
        return site_map

    def _check_survey_floor(robot, map_id, floor_index: int) -> None:
        """A floor the map could never take is refused now, not by the SiteMap
        validator after the whole survey has been recorded and processed: a new
        map starts at floor 0, and a survey of an existing map redoes one of
        its floors or adds the next one up (floors run 0..n-1, no gaps)."""
        if map_id is None:
            if floor_index != 0:
                raise HTTPException(
                    status_code=422,
                    detail=(f"a new map starts at floor 0, not floor {floor_index}; "
                            "give the floor a name instead of a number"))
            return
        if map_lookup is None:
            return
        site_map = _map_at_robot_site(robot, map_id)
        if floor_index > len(site_map.floors):
            count = len(site_map.floors)
            raise HTTPException(
                status_code=422,
                detail=(f"map {map_id} has {count} floor{'' if count == 1 else 's'}, so a survey of it "
                        f"can be floors 0–{count}: one of them again, or the next one up. "
                        f"Floor {floor_index} would leave a gap"))

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
        if command_type == "START_SURVEY":
            _check_survey_floor(robot, parsed.map_id, parsed.floor_index)
            return
        map_id = getattr(parsed, "map_id", None)
        if map_id is None or map_lookup is None:
            return
        site_map = _map_at_robot_site(robot, map_id)
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

    def _check_map_ready(http: Request, robot_id: str, command_type: str, params) -> None:
        """422 / 409 / 404 / 403 for a map ready checklist command (0.0.29)
        the robot would refuse anyway, or that names what this caller may not
        touch. Whether the robot is idle enough is the robot's to judge."""
        model, noun, what = _MAP_READY_PARAMS[command_type]
        try:
            parsed = model.model_validate(params or {})
        except ValidationError as exc:
            raise HTTPException(status_code=422,
                                detail=f"{command_type} params are not {noun} ({_bad_fields(exc)})") from exc
        robot = registry.get(robot_id) if registry is not None else None
        if robot is not None:
            version = _parse_version(robot.schema_version)
            if version is None or version < _parse_version(MAP_READY_SCHEMA_VERSION):
                raise HTTPException(
                    status_code=409,
                    detail=(f"update the edge device: it runs schema {robot.schema_version or 'unknown'}, "
                            f"{what} needs {MAP_READY_SCHEMA_VERSION}"))
        if command_type == "REPROCESS_SURVEY":
            _check_reprocess(robot_id, robot, parsed)
        elif command_type == "USE_NAV_MAP":
            _check_use_nav_map(http, robot_id, robot, parsed)
        elif command_type == "SET_SITE":
            _check_set_site(http, robot_id, robot, parsed)

    def _check_reprocess(robot_id: str, robot, parsed: ReprocessSurveyParams) -> None:
        # Processing again needs the survey's bag, and the robot lists the
        # recordings it holds with its registration (0.0.29).
        if robot is not None:
            listed = next((r for r in robot.recordings if r.get("survey_id") == parsed.survey_id), None)
            if listed is None:
                raise HTTPException(
                    status_code=409,
                    detail=(f"the recording is no longer on the robot: {robot_id} does not list "
                            f"survey {parsed.survey_id}"))
            if not listed.get("has_bag"):
                raise HTTPException(
                    status_code=409,
                    detail=(f"the recording is no longer on the robot: {robot_id} keeps survey "
                            f"{parsed.survey_id}'s results but not its bag, so it cannot be processed again"))
        _check_survey_floor(robot, parsed.map_id, parsed.floor_index)

    def _check_use_nav_map(http: Request, robot_id: str, robot, parsed: UseNavMapParams) -> None:
        # Unlike SYNC_MAP, a robot on another map -- or on none -- is exactly
        # what this is for. The robot checks the hash of what it builds again.
        if map_lookup is None:
            return
        site_map = map_lookup(parsed.map_id)
        robot_site = robot.site_id if robot is not None else None
        if site_map is not None and robot_site is not None and site_map.site_id != robot_site:
            # A map belongs to one site, so the robot moves there first
            # (SET_SITE). Said only to a caller who may see that site; to
            # anyone else the map reads as not found.
            if site_visible(http, site_map.site_id):
                raise HTTPException(
                    status_code=409,
                    detail=(f"map {parsed.map_id} is at site {site_map.site_id} and {robot_id} is at "
                            f"{robot_site}: move the robot to {site_map.site_id} first"))
            site_map = None
        if site_map is None:
            raise HTTPException(status_code=404, detail=f"map {parsed.map_id} not found")
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
                detail=(f"the map changed since you opened it: map {parsed.map_id} floor {parsed.floor_index} "
                        f"is now version {floor.content_hash[:12]}, not {parsed.map_hash[:12]} -- reload it "
                        "and try again"))

    def _check_set_site(http: Request, robot_id: str, robot, parsed: SetSiteParams) -> None:
        # The command's capability was checked at the robot's own site before
        # this (the route's site is the robot's); the site it moves to is
        # checked here, so a move needs `configure` at both.
        if robot is not None and robot.site_id == parsed.site_id:
            raise HTTPException(status_code=409, detail=f"{robot_id} is already at site {parsed.site_id}")
        if site_known is not None and not site_known(parsed.site_id):
            raise HTTPException(status_code=404, detail=f"site {parsed.site_id} not found")
        require_site_capability(http, parsed.site_id, command_capabilities("SET_SITE"))

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
        elif request.type in _MAP_READY_PARAMS:
            _check_map_ready(http, robot_id, request.type, request.params)
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
