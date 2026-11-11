"""What the backend checks about a program's Stairs steps (schema 0.0.21).

A Stairs step (shared/schemas/missions.py StairsStep) names a `stairs` zone on
the mission's map; the bridge walks the flight from its own copy of that zone
(robot_bridge/stairs_step.py). This is the early, friendly refusal -- the bridge
is the safety authority and checks it all again -- worded as one sentence the
console shows verbatim, like missions/route_steps.py's `R_*`.

Two moments:

  * saving a program (missions/router.py): Stairs allowed must be on (stairs
    hand-off: "a mission with a Stairs step can't be saved or started unless
    it is true"), and, when the router has the map store, each step's zone must
    be a stairs zone on the mission's map;
  * starting a run (runs/dispatch.py): all of that, plus the map is active, the
    robot is four-legged, its bridge knows Stairs steps (0.0.21) and is on that
    map, and the landing the step starts from can be walked to (clear of the
    bridge's zone margin and of every other zone).

Pure: no store, no I/O.

Today the bridge refuses every flight anyway (the SDK has no stair motion for
a point-foot robot: REJECTED_STAIRS_NOT_SUPPORTED). These checks are what a
mission will meet once it can climb, and they keep a bad reference from
reaching a robot meanwhile.
"""
from __future__ import annotations

import math
from typing import Iterable, Iterator, List, Optional

from missions.route_steps import ZONE_MARGIN_M
from shared.schemas.maps import MapZone, SiteMap
from shared.schemas.missions import MissionLibraryEntry

# The first schema whose bridge knows a Stairs step.
STAIRS_SCHEMA_VERSION = "0.0.21"

R_STAIRS_NOT_ALLOWED = ("this program climbs stairs (the step {step}), but Stairs allowed is off; "
                        "turn it on in the mission's settings")
R_STAIRS_NO_ZONE = ("the Stairs step {step} names the stairs {zone}, which the map {map_name} does not have; "
                    "pick a staircase drawn on that map in Missions › Maps")
R_STAIRS_NOT_STAIRS = ("the Stairs step {step} names the zone {zone}, which is a {kind} zone, not a staircase; "
                       "pick a staircase in the mission editor")
R_STAIRS_MAP_MISSING = ("the map {map_id} this program's stairs are drawn on is not in the map store; "
                        "upload it in Missions › Maps")
R_STAIRS_MAP_DRAFT = "the map {map_name} with this program's stairs is a draft; activate it in Missions › Maps"
R_STAIRS_MAP_ARCHIVED = ("the map {map_name} with this program's stairs is archived; "
                         "make it active again in Missions › Maps")
R_STAIRS_NOT_QUAD = ("Stairs steps run only on four-legged robots for now, and this robot is a "
                     "{robot_type} robot; pick a four-legged robot to run it")
R_STAIRS_SCHEMA = ("update the edge device: it runs schema {version}, Stairs steps need "
                   + STAIRS_SCHEMA_VERSION)
R_STAIRS_NAV_MAP = ("{robot_id} is localised on the map {robot_map}, not on {map_name}, which has this "
                    "program's stairs; load that map on the robot or pick a robot that has it")
R_STAIRS_LANDING = ("the {end} landing of the stairs {zone} is within {margin:g} m of {other}, so the robot "
                    "cannot walk to it; move the landing in Missions › Maps")


class StairsNotRunnable(Exception):
    """A Stairs step cannot run as it is; `message` is the sentence to show."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def stairs_steps(steps: Iterable) -> Iterator:
    """Every Stairs step, however deeply nested."""
    for step in steps:
        kind = getattr(step, "kind", None)
        if kind == "stairs":
            yield step
        elif kind in ("repeat", "repeatUntil"):
            yield from stairs_steps(step.body)
        elif kind == "if":
            yield from stairs_steps(step.then)
            yield from stairs_steps(step.else_)


def program_stairs(mission: MissionLibraryEntry) -> List:
    """The Stairs steps in a stored program's compiled steps, both robot types."""
    compiled = mission.program_steps
    if compiled is None:
        return []
    return [*stairs_steps(compiled.quad), *stairs_steps(compiled.wheel)]


def _name(step) -> str:
    return (step.label or "").strip() or step.blockId


def _zone_name(zone: MapZone) -> str:
    return zone.label.strip() or zone.zone_id


def check_allowed(mission: MissionLibraryEntry, flights: List) -> None:
    """Stairs allowed must be on for a mission that climbs. Raises StairsNotRunnable."""
    if flights and not mission.constraints.stairs_allowed:
        raise StairsNotRunnable(R_STAIRS_NOT_ALLOWED.format(step=_name(flights[0])))


def check_zones(flights: List, site_map: SiteMap) -> None:
    """Every step names a stairs zone on `site_map`. Raises StairsNotRunnable."""
    zones = {z.zone_id: z for z in site_map.zones}
    for step in flights:
        zone = zones.get(step.zoneId)
        if zone is None:
            raise StairsNotRunnable(R_STAIRS_NO_ZONE.format(step=_name(step), zone=step.zoneId,
                                                            map_name=site_map.name))
        if zone.kind != "stairs" or zone.stairs is None:
            raise StairsNotRunnable(R_STAIRS_NOT_STAIRS.format(step=_name(step), zone=_zone_name(zone),
                                                               kind=zone.kind.replace("_", "-")))


def _inside(x: float, y: float, polygon) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        (x1, y1), (x2, y2) = polygon[i], polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
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


def check_landings(flights: List, site_map: SiteMap) -> None:
    """The landing each step starts from can be walked to: clear of the
    bridge's margin around its own flight and around every no-go or stairs
    zone (the bridge's own rule, robot_bridge/nav_zones.py), as a tapped goal
    or a route leg must be. Raises StairsNotRunnable."""
    zones = {z.zone_id: z for z in site_map.zones}
    for step in flights:
        flight = zones[step.zoneId]
        end = "bottom" if step.direction == "up" else "top"
        x, y = flight.stairs.bottom_xy_m if step.direction == "up" else flight.stairs.top_xy_m
        for zone in site_map.zones:
            if zone.kind not in ("no_go", "stairs"):
                continue
            if _edge_distance(x, y, zone.polygon) <= ZONE_MARGIN_M or _inside(x, y, zone.polygon):
                other = "its own flight" if zone is flight else f"the zone {_zone_name(zone)}"
                raise StairsNotRunnable(R_STAIRS_LANDING.format(
                    end=end, zone=_zone_name(flight), margin=ZONE_MARGIN_M, other=other))


def check_map(site_map: Optional[SiteMap], map_id: str) -> SiteMap:
    """The mission's map exists and is active. Raises StairsNotRunnable."""
    if site_map is None:
        raise StairsNotRunnable(R_STAIRS_MAP_MISSING.format(map_id=map_id))
    if site_map.status == "draft":
        raise StairsNotRunnable(R_STAIRS_MAP_DRAFT.format(map_name=site_map.name))
    if site_map.status != "active":
        raise StairsNotRunnable(R_STAIRS_MAP_ARCHIVED.format(map_name=site_map.name))
    return site_map
