"""Trim a floor to the walked area (map ready checklist, spec §4.5, 0.0.29).

FAST-LIO2 keeps every return it registers, and a few of them are far from
anywhere the robot went: through a window, down a corridor it only looked
along, a reflection. The grid used to span the whole cloud (less the 0.1 %
sparsest returns, survey_math.trim_bounds), so on 2026-11-20 a handful of far
points made the 15:08 survey's canvas 58 m wide (x -43.9..14.1) for an office
about 11 m across. Nothing in it past the far wall was a place the robot could
be sent, and the canvas the console draws and the files a robot loads were
five times the size they needed to be.

Two halves:

- automatic, for a NEW floor (maps/survey.py): only the points within
  AUTO_TRIM_MARGIN_M (30 m) of the walked track (x, y) make the grid
  (`near_track`), and its extent is held inside the track's bounding box plus
  that margin (`track_bounds`). Wide, because a survey must work on any site:
  a mall atrium's real walls stand 15-30 m from the path, while the 15:08
  strays sat 40-58 m out. The robot's recording keeps the whole cloud.
- `trim_floor`, for a floor already stored (POST /maps/{id}/floors/{n}/trim):
  a person's decision, so the tighter TRIM_MARGIN_M (10 m): the floor is cut
  down to the track's bounding box plus that margin, in whole
  cells of its own grid, as a NEW VERSION through MapStore.edit_floor -- the
  bundle it replaces is kept and restorable, the map drops to draft, and the
  router queues the quality check. Every per-floor file that has a place is
  cut with it: the grid and its preview, the planning grid, the cloud sample,
  the "probably moved" layer and its points. The track is inside by
  construction; survey.json describes how the floor was made and is left as it
  was.

What a trim refuses (TrimRefused, a 409): a floor with no track (a map
uploaded by hand), a track that lies outside the floor, and a zone the new
edge would cut off. Zones belong to the map, not to a floor, so a zone counts
as this floor's when it overlaps the floor's old rectangle at all; a zone over
another floor's area is left alone.

The content hash changes (it covers the grids and their placement); the floor's
label, the map's zones and their review do not.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from shared.schemas.maps import MapFloor, MapZone, SiteMap

from .store import (
    ARTIFACT_FILES, MAX_CLOUD_BYTES, BundleError, MapNotFound, MapStore, count_cells, parse_pgm, planning_grid_for,
)

# How far past the walked track (x, y) the Trim button leaves a floor. Ten
# metres is a room's width beyond a doorway the robot stopped in -- what a
# survey can see and a robot might later be sent to.
TRIM_MARGIN_M = 10.0
# The automatic cut at survey time (maps/survey.py) reaches three times as
# far. A survey must work on any site: a shopping-mall atrium's walls stand
# 15-30 m from the walked path, and Mid-360 returns are useful for
# localisation out to about 30 m. The strays that stretched the 15:08 survey
# sat 40-58 m out, so 30 m still drops them.
AUTO_TRIM_MARGIN_M = 30.0
# The near-track mask's cell. Coarse on purpose: it decides which returns are
# near enough to keep, not where anything is drawn.
NEAR_TRACK_CELL_M = 0.5
# Cells marked per numpy step (track cells x offsets), which bounds the
# scratch memory: at 30 m and 0.5 m cells there are ~12,000 offsets, so a
# step is a few hundred track cells, 32 MB of int64.
_MARKS_PER_CHUNK = 4_000_000
# Float slack when a bound is turned into whole cells: (-8 - -44) / 0.1 is
# 359.99999999999994, which is cell 360.
_EPS = 1e-6

Bounds = Tuple[float, float, float, float]


class TrimRefused(BundleError):
    """The floor cannot be trimmed as asked. The message is safe to show."""


class _Unchanged(Exception):
    """The floor already lies within the margin: nothing to store."""


def track_bounds(track_xy, margin_m: float = TRIM_MARGIN_M) -> Bounds:
    """(x_lo, y_lo, x_hi, y_hi): the track's bounding box grown by `margin_m`."""
    import numpy as np

    xy = np.asarray(track_xy, dtype=np.float64)
    xy = xy[np.all(np.isfinite(xy), axis=1)]
    if not len(xy):
        raise TrimRefused("the walked track has no positions")
    lo, hi = xy.min(axis=0), xy.max(axis=0)
    return (float(lo[0]) - margin_m, float(lo[1]) - margin_m, float(hi[0]) + margin_m, float(hi[1]) + margin_m)


def near_track(points_xy, track_xy, margin_m: float = TRIM_MARGIN_M,
               cell_m: float = NEAR_TRACK_CELL_M):
    """Which of `points_xy` lie within `margin_m` of the walked track.

    Done on a coarse grid: a point is kept when its cell is within the margin
    of a cell the track passed through, cell edge to cell edge, so nothing
    within the margin is ever dropped and nothing beyond margin + 2 x 0.71
    cells is kept. A bool mask, one per point. An empty track keeps everything
    (there is nothing to measure from)."""
    import numpy as np

    points_xy = np.asarray(points_xy, dtype=np.float64)
    track_xy = np.asarray(track_xy, dtype=np.float64)
    track_xy = track_xy[np.all(np.isfinite(track_xy), axis=1)]
    if not len(track_xy):
        return np.ones(len(points_xy), dtype=bool)
    reach = int(math.ceil(margin_m / cell_m)) + 1
    pad = reach + 1
    x0 = float(track_xy[:, 0].min()) - pad * cell_m
    y0 = float(track_xy[:, 1].min()) - pad * cell_m
    cols = int((float(track_xy[:, 0].max()) - x0) / cell_m) + pad + 2
    rows = int((float(track_xy[:, 1].max()) - y0) / cell_m) + pad + 2

    # Cell offsets whose nearest edges are within the margin.
    span = np.arange(-reach, reach + 1)
    dc, dr = np.meshgrid(span, span)
    gap = np.hypot(np.maximum(np.abs(dc) - 1, 0), np.maximum(np.abs(dr) - 1, 0)) * cell_m
    offsets = (dr * cols + dc)[gap <= margin_m]

    track_cells = np.unique(((track_xy[:, 1] - y0) / cell_m).astype(np.int64) * cols
                            + ((track_xy[:, 0] - x0) / cell_m).astype(np.int64))
    mask = np.zeros(rows * cols, dtype=bool)
    per_chunk = max(1, _MARKS_PER_CHUNK // len(offsets))
    for start in range(0, len(track_cells), per_chunk):
        chunk = track_cells[start:start + per_chunk]
        mask[(chunk[:, None] + offsets[None, :]).ravel()] = True

    keep = np.zeros(len(points_xy), dtype=bool)
    with np.errstate(invalid="ignore"):
        col = np.floor((points_xy[:, 0] - x0) / cell_m)
        row = np.floor((points_xy[:, 1] - y0) / cell_m)
    on_grid = (col >= 0) & (col < cols) & (row >= 0) & (row < rows)
    index = row[on_grid].astype(np.int64) * cols + col[on_grid].astype(np.int64)
    keep[np.flatnonzero(on_grid)] = mask[index]
    return keep


def _floor_rect(floor: MapFloor) -> Bounds:
    width, height = floor.size_cells
    x0, y0 = floor.origin_xy_m
    return (x0, y0, x0 + width * floor.resolution_m, y0 + height * floor.resolution_m)


def _window(floor: MapFloor, bounds: Bounds) -> Optional[Tuple[int, int, int, int]]:
    """(c0, c1, j0, j1): the floor's whole cells covering `bounds`, columns
    c0..c1-1 from the left and cell rows j0..j1-1 from the bottom, clipped to
    the floor. None when `bounds` misses the floor."""
    res = floor.resolution_m
    x0, y0 = floor.origin_xy_m
    width, height = floor.size_cells
    c0 = max(0, int(math.floor((bounds[0] - x0) / res + _EPS)))
    c1 = min(width, int(math.ceil((bounds[2] - x0) / res - _EPS)))
    j0 = max(0, int(math.floor((bounds[1] - y0) / res + _EPS)))
    j1 = min(height, int(math.ceil((bounds[3] - y0) / res - _EPS)))
    if c1 <= c0 or j1 <= j0:
        return None
    return c0, c1, j0, j1


def _inside(xy, rect: Bounds):
    import numpy as np

    xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    return ((xy[:, 0] >= rect[0] - _EPS) & (xy[:, 0] <= rect[2] + _EPS)
            & (xy[:, 1] >= rect[1] - _EPS) & (xy[:, 1] <= rect[3] + _EPS))


def zones_cut(zones: Sequence[MapZone], old: Bounds, new: Bounds) -> List[MapZone]:
    """The zones a trim from `old` to `new` would cut: those overlapping the
    old rectangle with any vertex (or stairs landing) outside the new one."""
    cut = []
    for zone in zones:
        points = [tuple(p) for p in zone.polygon]
        if zone.stairs is not None:
            points += [tuple(zone.stairs.bottom_xy_m), tuple(zone.stairs.top_xy_m)]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        overlaps = min(xs) <= old[2] and max(xs) >= old[0] and min(ys) <= old[3] and max(ys) >= old[1]
        if overlaps and not bool(_inside(points, new).all()):
            cut.append(zone)
    return cut


def _crop_pgm(data: bytes, floor: MapFloor, window, what: str):
    """The pixels of a floor-sized PGM inside `window`, as a (rows, cols) array."""
    import numpy as np

    try:
        width, height, pixels = parse_pgm(data)
    except BundleError as exc:
        raise BundleError("floor %d's %s: %s" % (floor.index, what, exc)) from None
    if (width, height) != tuple(floor.size_cells):
        raise BundleError("floor %d's %s is not the floor's size" % (floor.index, what))
    c0, c1, j0, j1 = window
    image = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width)
    return np.ascontiguousarray(image[height - j1:height - j0, c0:c1])


def trim_floor(store: MapStore, map_id: str, index: int,
               margin_m: float = TRIM_MARGIN_M) -> Tuple[SiteMap, Optional[int]]:
    """Cut floor `index` of `map_id` down to its walked track's bounding box
    plus `margin_m`, as a new draft version: (the map, the version it made,
    as edit_floor read it under the store's lock). A floor already inside
    that is returned unchanged with None: no new version. Raises
    TrimRefused (no track, a track off the floor, a zone it would cut),
    BundleError (a floor whose files do not agree), MapNotFound / FloorNotFound."""
    import numpy as np

    from . import survey_math as sm
    from .cloud_edit import moved_bytes, parse_moved, pcd_bytes, read_pcd_bytes

    detail: dict = {"floor": index, "margin_m": margin_m}

    def change(current: SiteMap, floor: MapFloor, floor_dir: Path, write) -> dict:
        track_path = floor_dir / ARTIFACT_FILES["track"]
        if not track_path.is_file():
            raise TrimRefused("floor %d has no walked track to trim to (it was not made from a survey)" % index)
        try:
            track = sm.read_track_csv(track_path.read_bytes())
        except ValueError as exc:  # SurveyError
            raise TrimRefused("floor %d's walked track cannot be read: %s" % (index, exc)) from None
        window = _window(floor, track_bounds(track[:, 1:3], margin_m))
        if window is None:
            raise TrimRefused("floor %d's walked track lies outside the floor; it cannot be trimmed to it" % index)
        c0, c1, j0, j1 = window
        width, height = floor.size_cells
        if (c0, c1, j0, j1) == (0, width, 0, height):
            raise _Unchanged()
        res = floor.resolution_m
        origin = (round(floor.origin_xy_m[0] + c0 * res, 6), round(floor.origin_xy_m[1] + j0 * res, 6))
        trimmed = floor.model_copy(update={"origin_xy_m": origin, "size_cells": (c1 - c0, j1 - j0)})
        old_rect, new_rect = _floor_rect(floor), _floor_rect(trimmed)

        cut = zones_cut(current.zones, old_rect, new_rect)
        if cut:
            more = " (and %d more)" % (len(cut) - 1) if len(cut) > 1 else ""
            raise TrimRefused("zone %s lies outside the trimmed floor%s: trimming would cut it off. Move or "
                              "delete it first" % (cut[0].label, more))

        grid = _crop_pgm((floor_dir / ARTIFACT_FILES["grid"]).read_bytes(), floor, window, "grid")
        write("grid", sm.pgm_bytes(grid))
        write("preview", sm.png_bytes(grid))
        free, occupied, unknown = count_cells(grid.tobytes())
        updates: dict = {"origin_xy_m": origin, "size_cells": (c1 - c0, j1 - j0),
                         "free_cells": free, "occupied_cells": occupied, "unknown_cells": unknown}

        planning_path = floor_dir / ARTIFACT_FILES["planning"]
        if floor.planning is not None:
            if planning_path.is_file():
                planning = sm.pgm_bytes(_crop_pgm(planning_path.read_bytes(), floor, window, "planning grid"))
                updates["planning"] = planning_grid_for(trimmed, planning, floor.planning.clearance_m)
                write("planning", planning)
            else:
                # Described but not stored: its counts are the old size's and
                # would no longer fill the floor. No planning grid, then.
                updates["planning"] = None

        cloud_path = floor_dir / ARTIFACT_FILES["cloud"]
        points_dropped = 0
        if cloud_path.is_file():
            raw = cloud_path.read_bytes()
            if len(raw) % 12:
                raise BundleError("floor %d's cloud is not whole float32 x, y, z triples" % index)
            cloud = np.frombuffer(raw, dtype="<f4").reshape(-1, 3)
            keep = _inside(cloud[:, :2], new_rect)
            points_dropped = int((~keep).sum())
            if points_dropped:
                write("cloud", np.ascontiguousarray(cloud[keep]).tobytes())

        moved_path = floor_dir / ARTIFACT_FILES["moved"]
        blobs_dropped: List[str] = []
        if moved_path.is_file():
            moved = parse_moved(moved_path.read_bytes())
            kept = [b for b in moved["blobs"] if bool(_inside(b["centroid"], new_rect)[0])]
            kept_ids = {b["id"] for b in kept}
            blobs_dropped = [b["id"] for b in moved["blobs"] if b["id"] not in kept_ids]
            if blobs_dropped:
                write("moved", moved_bytes({**moved, "blobs": kept}))
        moved_cloud_path = floor_dir / ARTIFACT_FILES["moved_cloud"]
        if moved_cloud_path.is_file():
            points = read_pcd_bytes(moved_cloud_path.read_bytes(), MAX_CLOUD_BYTES // 12)
            keep = _inside(points[:, :2], new_rect)
            if not keep.all():
                write("moved_cloud", pcd_bytes(points[keep]))

        # Read by edit_floor into version.json after this returns.
        detail.update({"from_size_cells": [width, height], "to_size_cells": [c1 - c0, j1 - j0],
                       "from_origin_xy_m": list(floor.origin_xy_m), "to_origin_xy_m": list(origin),
                       "cloud_points_dropped": points_dropped, "moved_blobs_dropped": blobs_dropped[:50]})
        return updates

    try:
        return store.edit_floor(map_id, index, change, source="trim", detail=detail)
    except _Unchanged:
        current = store.get(map_id)
        if current is None:  # deleted in between
            raise MapNotFound(map_id) from None
        return current, None
