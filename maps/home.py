"""Where a floor's home may be (schema 0.0.33): the check behind PUT
/maps/{id}/floors/{i}/home.

Return home walks the robot to home on the Nav2 path and leaves it facing the
home's heading, so home must be somewhere the planner can reach and the robot
can turn round on the spot. Both are judged on the floor's planning grid -- the
grid the planner uses -- where only cells the survey walked are free: a cell
that is occupied or was never walked is blocked, and so is anything off the
grid. Home's own cell must be free, and so must every cell whose centre lies
within HOME_CLEARANCE_M of home.

The map's zones are checked by the bridge's rules (robot_bridge/nav_zones.py),
because the bridge refuses a RETURN whose pose breaks them and a home it would
refuse is a button that never works: not inside or near a no-go or stairs
zone, and, when the map has geofences, well inside one. "Near" is
HOME_CLEARANCE_M rather than the bridge's 0.4 m margin, so the one figure
means "room round home" for walls and zones alike, and is never the smaller.
A zone has no floor of its own (MapZone), so every zone of the map applies to
every floor, as it does on the robot.

Pure Python on the PGM's bytes: a home check is a few hundred cells.

Spec: docs/superpowers/specs/2026-11-26-resume-after-estop-and-return-home-design.md
"""
from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

from shared.schemas.maps import MapFloor, MapZone

from .store import FREE_THRESH, BundleError, parse_pgm

# The bridge's own "something is close" distance for its tight-spot
# controller: the robot turns in place on arrival, and needs this much room
# round its centre to do it.
HOME_CLEARANCE_M = 0.45

Refusal = Tuple[str, str]  # (code, message), the 422's detail


class HomeRefused(Exception):
    """A home the floor cannot take. `code` and `message` are the 422's."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_free(value: int) -> bool:
    # Nav2's reading of a trinary PGM (store.OCCUPIED_THRESH): free only below
    # the free threshold. Unknown counts as blocked -- never walked.
    return (255.0 - value) / 255.0 < FREE_THRESH


def _segment_distance(px: float, py: float, a: Sequence[float], b: Sequence[float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _edge_distance(x: float, y: float, polygon: Sequence[Sequence[float]]) -> float:
    n = len(polygon)
    return min(_segment_distance(x, y, polygon[i], polygon[(i + 1) % n]) for i in range(n))


def _inside(x: float, y: float, polygon: Sequence[Sequence[float]]) -> bool:
    """Even-odd ray cast, the same rule as robot_bridge/nav_zones.py."""
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _zone_refusal(zones: Iterable[MapZone], x: float, y: float) -> Optional[Refusal]:
    zones = list(zones)
    for zone in zones:
        if zone.kind not in ("no_go", "stairs"):
            continue
        if _inside(x, y, zone.polygon) or _edge_distance(x, y, zone.polygon) < HOME_CLEARANCE_M:
            what = "no-go zone" if zone.kind == "no_go" else "stairs zone"
            return ("home_not_clear",
                    'Home is inside or within %.2f m of the %s "%s", where the robot may not walk.'
                    % (HOME_CLEARANCE_M, what, zone.label))
    fences: List[MapZone] = [z for z in zones if z.kind == "geofence"]
    if fences and not any(_inside(x, y, z.polygon) and _edge_distance(x, y, z.polygon) >= HOME_CLEARANCE_M
                          for z in fences):
        return ("home_not_clear",
                "Home must be at least %.2f m inside a geofence; the robot is refused a walk anywhere else."
                % HOME_CLEARANCE_M)
    return None


def check_home(floor: MapFloor, planning_grid: Optional[bytes], zones: Iterable[MapZone],
               x_m: float, y_m: float) -> Optional[Refusal]:
    """None if (x_m, y_m) may be `floor`'s home, else (code, message).

    `planning_grid` is the floor's planning.pgm (None: it has none). Codes:
    `home_no_planning_grid`, `home_not_clear`."""
    if planning_grid is None:
        return ("home_no_planning_grid",
                "Floor %d has no planning grid, so there is no walked area to check home against. "
                "Make one in the map editor (or survey the floor again) first." % floor.index)
    try:
        width, height, pixels = parse_pgm(planning_grid)
    except BundleError:
        # The store refuses such a file on the way in; one that is unreadable
        # now is as good as none.
        return ("home_no_planning_grid",
                "Floor %d's planning grid cannot be read, so home cannot be checked against it." % floor.index)
    res = floor.resolution_m
    ox, oy = floor.origin_xy_m

    def free(col: int, j: int) -> bool:
        if not (0 <= col < width and 0 <= j < height):
            return False
        # Row 0 of the PGM is the top of the map (max y), as Nav2 writes it.
        return _is_free(pixels[(height - 1 - j) * width + col])

    col = math.floor((x_m - ox) / res)
    j = math.floor((y_m - oy) / res)
    if not free(col, j):
        return ("home_not_clear",
                "Home is outside the walked area: the robot can only be sent where the survey walked.")

    reach = math.ceil(HOME_CLEARANCE_M / res) + 1
    for dj in range(-reach, reach + 1):
        for dc in range(-reach, reach + 1):
            cx = ox + (col + dc + 0.5) * res
            cy = oy + (j + dj + 0.5) * res
            if math.hypot(cx - x_m, cy - y_m) <= HOME_CLEARANCE_M and not free(col + dc, j + dj):
                return ("home_not_clear",
                        "A wall or an unwalked area is within %.2f m of home; the robot needs that much room "
                        "to turn round when it arrives." % HOME_CLEARANCE_M)
    return _zone_refusal(zones, x_m, y_m)
