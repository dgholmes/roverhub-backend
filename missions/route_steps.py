"""Compile a route mission into the steps the bridge runs (schema 0.0.16).

A route is geometry: waypoints in the map frame, the legs between them, and
actions at each waypoint. The bridge runs steps. This turns one into the other
when a run starts -- for Run now, the editor's test run and the scheduler alike
-- because only the backend has the map store, and the scheduler has no browser.
Design: docs/superpowers/specs/2026-11-11-route-missions-on-nav-design.md.

    waypoint             -> NavStep "wp:<id>:go" (walk there through the bridge's
                            nav path), then the waypoint's actions, in order:
    snapshot             -> CommandStep SNAPSHOT {camera}        "wp:<id>:a<k>"
    voice_prompt         -> CommandStep SPEAK {clip_id, volume}  "wp:<id>:a<k>"
    wait                 -> WaitStep(seconds)                    "wp:<id>:a<k>"
    segment into a waypoint: speed_mps -> that leg's maxSpeedMps

Everything the robot cannot do yet is refused with one sentence that names the
thing and what to do; the console shows it verbatim. Those sentences are the
`R_*` constants below.

Pure: no store, no I/O. The dispatcher (runs/dispatch.py) looks the map up,
checks it is active and that the robot is on it, then calls compile_route.

The zone check here is the early, friendly refusal, not the safety: the bridge
refuses a goal in a no-go zone or outside the boundary itself (nav_zones.py), and
the zones are burnt into the robot's planning grid as well. It uses the bridge's
own rules and margin so that the two agree on which points are refused.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from shared.schemas.commands import NavGotoParams
from shared.schemas.maps import MapFloor, MapZone, SiteMap
from shared.schemas.missions import (
    NAV_MIN_TOLERANCE_M, AnnouncementParams, CommandStep, MissionLibraryEntry, NavStep,
    ProgramStep, Segment, SnapshotParams, WaitParams, WaitStep, Waypoint,
)

# The bridge's NAV_ZONE_MARGIN_M default (robot_bridge/nav_zones.py, config.py):
# the body reaches 0.37 m from the point that is checked.
ZONE_MARGIN_M = 0.4
# NavStep's own upper bounds (shared/schemas/missions.py).
MAX_TOLERANCE_M = 5.0
MAX_SPEED_MPS = 1.0

# Per-leg time cap (design decision 10): 60 s plus 8 s per straight-line metre,
# held to 60..900 s. Where the robot starts the first leg is not known here, so
# that leg gets FIRST_LEG_S.
LEG_BASE_S = 60.0
LEG_PER_M_S = 8.0
LEG_MIN_S = 60.0
LEG_MAX_S = 900.0
FIRST_LEG_S = 300.0
# The run cap when the task sets none: legs plus actions, with this much slack.
RUN_CAP_FACTOR = 1.5
# What an action is allowed for in that sum. A clip's length is not known here.
SNAPSHOT_S = 5.0
SPEAK_S = 30.0

# Mission cameras are front/rear; the bridge's SNAPSHOT names front/back
# (robot_bridge/snapshots.py CAMERAS).
CAMERA_ON_ROBOT = {"front": "front", "rear": "back"}

# The console's own names for actions and policies (en i18n, missions.actions /
# missions.settings.proximity), so a refusal reads like the screen it points at.
ACTION_NAMES = {
    "video_clip": "Video", "led_signal": "LED Signal",
    "operator_approval": "Operator Approval", "return_to_dock": "Return to Dock",
}
PEOPLE_POLICY_WORDS = {
    "slow": "slow down", "pause": "pause", "alert": "alert an operator",
    "require_takeover": "hand over to an operator",
}

R_NOT_QUAD = ("routes run only on four-legged robots for now, and this robot is a {robot_type} robot; "
              "pick a four-legged robot to run it")
R_NO_WAYPOINTS = "this route has no waypoints; add at least one in the mission editor"
R_STAIRS = ("this route allows stairs, which the robot cannot climb on a route yet; "
            "turn off Stairs allowed in the mission's settings")
R_PEOPLE = ("this route asks the robot to {words} when people are near, which it cannot do yet; "
            "set When people are near to Carry on in the mission's settings")
R_ANNOUNCEMENTS = ("this route has timed announcements, which cannot play on the robot yet; "
                   "remove them in the mission's Announcements tab")
R_NO_FLOORS = "the map {map_name} has no surveyed floor; upload its survey again in Missions › Maps"
R_OFF_GRID = ("waypoint {waypoint} is outside the surveyed area of the map {map_name}; "
              "move it onto the map in the mission editor")
R_NO_GO = ("waypoint {waypoint} is in or within {margin:g} m of the no-go zone {zone}; "
           "move it further out in the mission editor")
R_IN_STAIRS = ("waypoint {waypoint} is on or within {margin:g} m of the stairs {zone}, which only a Stairs "
               "step may cross; move it onto a landing in the mission editor")
R_OUTSIDE = ("waypoint {waypoint} is outside the site boundary {zone}, or within {margin:g} m of its edge; "
             "move it inside in the mission editor")
R_ACTION = ("the {action} action at waypoint {waypoint} cannot run on the robot yet; "
            "remove it from the route to run this mission")
R_NO_CLIP = ("the announcement at waypoint {waypoint} has nothing to say yet; "
             "record, type or pick a clip in the mission editor")
R_GAIT = ("the leg into waypoint {waypoint} uses the {gait} gait, and routes can only walk for now; "
          "set its gait back to walk in the mission editor")
R_POSTURE = ("the leg into waypoint {waypoint} sets a posture, which routes cannot do yet; "
             "clear it in the mission editor")
R_WHEEL_LEG = ("the leg into waypoint {waypoint} sets a wheel-leg mode, which routes cannot do yet; "
               "clear it in the mission editor")


class RouteNotRunnable(Exception):
    """The route cannot run as it is; `message` is the sentence to show."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class CompiledRoute:
    steps: List[ProgramStep]
    # The run's time cap to use when the task (or the caller) sets none.
    cap_s: float


def heading_to_yaw(heading_deg: Optional[float]) -> Optional[float]:
    """A waypoint's `heading_deg` as the goal's `yaw_rad`.

    `heading_deg` is a compass bearing: degrees clockwise from the top of the
    map, which is map +y. `yaw_rad` is radians anticlockwise from map +x. So
    yaw = 90 deg - heading, wrapped to (-pi, pi]: 0 (up) -> pi/2, 90 (right)
    -> 0, 180 -> -pi/2, 270 -> pi.

    The evidence, since the editor draws no heading arrow on the map
    (route-editor/EditableRouteLayer.tsx draws none):
    - shared/schemas/missions.py, Waypoint.heading_deg: "0.0 ... means face
      map-frame north".
    - The editor's only control is StepInspector.tsx:89-90, "Face (deg)", a
      number from 0 to 359 in steps of 15, empty meaning "any direction"
      (en/missions.ts:174-176) -- a compass dial, not a signed yaw.
    - North is map +y, drawn up the screen: the console's map is Leaflet
      CRS.Simple with [lat, lng] = [y, x] (liveops/mapCoords.ts:9-11), and its
      arrows are drawn "pointing up (+y)" (mapCoords.ts:14).
    - The other heading in the console, RobotPosition.heading_deg, is
      anticlockwise from +x, and mapCoords.ts:20-21 turns it into a CSS
      rotation as 90 - heading. A waypoint's bearing IS that CSS rotation
      (clockwise from up), so an arrow drawn rotate(heading_deg) faces the way
      this yaw faces.

    None means "any heading": the bridge then faces the way the robot walked.
    """
    if heading_deg is None:
        return None
    yaw = math.radians(90.0 - heading_deg)
    # Wrap to (-pi, pi].
    yaw = math.atan2(math.sin(yaw), math.cos(yaw))
    if math.isclose(yaw, -math.pi):
        yaw = math.pi
    return yaw


def _name(waypoint: Waypoint) -> str:
    return waypoint.label.strip() or waypoint.waypoint_id


def _zone_name(zone: MapZone) -> str:
    return zone.label.strip() or zone.zone_id


# -- geometry, as robot_bridge/nav_zones.py does it -----------------------------------

def _inside(x: float, y: float, polygon) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _edge_distance(x: float, y: float, polygon) -> float:
    best = math.inf
    n = len(polygon)
    for i in range(n):
        (ax, ay), (bx, by) = polygon[i], polygon[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


def _zone_refusal(waypoint: Waypoint, zones: List[MapZone]) -> Optional[str]:
    x, y = waypoint.x_m, waypoint.y_m
    for zone in zones:
        if zone.kind == "no_go" and (
                _edge_distance(x, y, zone.polygon) <= ZONE_MARGIN_M or _inside(x, y, zone.polygon)):
            return R_NO_GO.format(waypoint=_name(waypoint), margin=ZONE_MARGIN_M, zone=_zone_name(zone))
    for zone in zones:
        # A flight of stairs (0.0.21): the bridge refuses a goal on one
        # (REJECTED_NAV_GOAL_IN_STAIRS_ZONE) with the same margin.
        if zone.kind == "stairs" and (
                _edge_distance(x, y, zone.polygon) <= ZONE_MARGIN_M or _inside(x, y, zone.polygon)):
            return R_IN_STAIRS.format(waypoint=_name(waypoint), margin=ZONE_MARGIN_M, zone=_zone_name(zone))
    fences = [z for z in zones if z.kind == "geofence"]
    if fences and not any(
            0.0 < _edge_distance(x, y, z.polygon) and _edge_distance(x, y, z.polygon) >= ZONE_MARGIN_M
            and _inside(x, y, z.polygon) for z in fences):
        names = ", ".join(_zone_name(z) for z in fences)
        return R_OUTSIDE.format(waypoint=_name(waypoint), zone=names, margin=ZONE_MARGIN_M)
    return None


def _floor_for(waypoint: Waypoint, floors: List[MapFloor]) -> MapFloor:
    if waypoint.z_m is None or len(floors) == 1:
        return floors[0]
    return min(floors, key=lambda f: (abs(f.floor_z_m - waypoint.z_m), f.index))


def _on_grid(waypoint: Waypoint, floor: MapFloor) -> bool:
    x0, y0 = floor.origin_xy_m
    width, height = floor.size_cells
    return (x0 <= waypoint.x_m <= x0 + width * floor.resolution_m
            and y0 <= waypoint.y_m <= y0 + height * floor.resolution_m)


# -- compiling ------------------------------------------------------------------------

def _leg_seconds(previous: Optional[Waypoint], waypoint: Waypoint) -> float:
    if previous is None:
        return FIRST_LEG_S
    metres = math.hypot(waypoint.x_m - previous.x_m, waypoint.y_m - previous.y_m)
    return min(LEG_MAX_S, max(LEG_MIN_S, LEG_BASE_S + LEG_PER_M_S * metres))


def _segment_into(segments: List[Segment], previous: Optional[Waypoint], waypoint: Waypoint) -> Optional[Segment]:
    if previous is None:
        return None
    for segment in segments:
        if segment.from_waypoint_id == previous.waypoint_id and segment.to_waypoint_id == waypoint.waypoint_id:
            return segment
    return None


def _speed_cap(segment: Optional[Segment], waypoint: Waypoint) -> Optional[float]:
    if segment is None:
        return None
    if segment.gait not in (None, "walk"):
        raise RouteNotRunnable(R_GAIT.format(waypoint=_name(waypoint), gait=segment.gait))
    if segment.posture:
        raise RouteNotRunnable(R_POSTURE.format(waypoint=_name(waypoint)))
    if segment.wheel_leg_mode is not None:
        raise RouteNotRunnable(R_WHEEL_LEG.format(waypoint=_name(waypoint)))
    speed = segment.speed_mps
    if speed is None or not math.isfinite(speed) or speed <= 0:
        return None
    # Above NavStep's 1 m/s is held there: the cap is a ceiling, and the
    # bridge's own limit applies under it anyway.
    return min(speed, MAX_SPEED_MPS)


def _actions(waypoint: Waypoint) -> tuple[List[ProgramStep], float]:
    steps: List[ProgramStep] = []
    seconds = 0.0
    for k, action in enumerate(waypoint.actions):
        block_id = f"wp:{waypoint.waypoint_id}:a{k}"
        if action.type == "snapshot":
            params = action.typed_params()
            assert isinstance(params, SnapshotParams)
            steps.append(CommandStep(kind="command", blockId=block_id, label=action.label, type="SNAPSHOT",
                                     params={"camera": CAMERA_ON_ROBOT[params.camera]}))
            seconds += SNAPSHOT_S
        elif action.type == "voice_prompt":
            params = action.typed_params()
            assert isinstance(params, AnnouncementParams)
            if not params.clip_id:
                raise RouteNotRunnable(R_NO_CLIP.format(waypoint=_name(waypoint)))
            steps.append(CommandStep(kind="command", blockId=block_id, label=action.label, type="SPEAK",
                                     params={"clip_id": params.clip_id, "volume": params.volume}))
            seconds += SPEAK_S
        elif action.type == "wait":
            params = action.typed_params()
            assert isinstance(params, WaitParams)
            steps.append(WaitStep(kind="wait", blockId=block_id, label=action.label, seconds=params.seconds))
            seconds += params.seconds
        else:
            name = ACTION_NAMES.get(action.type, action.type)
            raise RouteNotRunnable(R_ACTION.format(action=name, waypoint=_name(waypoint)))
    return steps, seconds


def compile_route(mission: MissionLibraryEntry, site_map: SiteMap, robot_type: Optional[str]) -> CompiledRoute:
    """The route's steps and its default run cap. Raises RouteNotRunnable.

    `site_map` is the mission's map; the caller has already checked it is the
    right one and active."""
    if robot_type != "quad":
        raise RouteNotRunnable(R_NOT_QUAD.format(robot_type=robot_type or "unknown"))
    if not mission.waypoints:
        raise RouteNotRunnable(R_NO_WAYPOINTS)
    constraints = mission.constraints
    if constraints.stairs_allowed:
        raise RouteNotRunnable(R_STAIRS)
    if constraints.human_proximity_policy != "none":
        words = PEOPLE_POLICY_WORDS.get(constraints.human_proximity_policy, constraints.human_proximity_policy)
        raise RouteNotRunnable(R_PEOPLE.format(words=words))
    if mission.announcements:
        raise RouteNotRunnable(R_ANNOUNCEMENTS)
    if not site_map.floors:
        raise RouteNotRunnable(R_NO_FLOORS.format(map_name=site_map.name))

    steps: List[ProgramStep] = []
    total_s = 0.0
    previous: Optional[Waypoint] = None
    for waypoint in mission.waypoints:
        floor = _floor_for(waypoint, site_map.floors)
        if not _on_grid(waypoint, floor):
            raise RouteNotRunnable(R_OFF_GRID.format(waypoint=_name(waypoint), map_name=site_map.name))
        refusal = _zone_refusal(waypoint, site_map.zones)
        if refusal is not None:
            raise RouteNotRunnable(refusal)
        speed = _speed_cap(_segment_into(mission.segments, previous, waypoint), waypoint)
        leg_s = _leg_seconds(previous, waypoint)
        steps.append(NavStep(
            kind="navigate", blockId=f"wp:{waypoint.waypoint_id}:go", label=waypoint.label,
            waypointId=waypoint.waypoint_id,
            goal=NavGotoParams(x_m=waypoint.x_m, y_m=waypoint.y_m, yaw_rad=heading_to_yaw(waypoint.heading_deg),
                               map_id=mission.map_id, floor_id=str(floor.index),
                               # The floor's version as stored now (0.0.20): the
                               # bridge refuses the leg if its files differ.
                               map_hash=floor.content_hash),
            toleranceM=min(MAX_TOLERANCE_M, max(waypoint.tolerance_m, NAV_MIN_TOLERANCE_M)),
            maxSeconds=leg_s, maxSpeedMps=speed,
        ))
        actions, action_s = _actions(waypoint)
        steps.extend(actions)
        total_s += leg_s + action_s
        previous = waypoint
    return CompiledRoute(steps=steps, cap_s=total_s * RUN_CAP_FACTOR)
