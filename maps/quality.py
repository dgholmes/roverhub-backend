"""A map's quality report: the numbers a reviewer needs before freezing it.

Tracker 1-5-05. Computed from the stored files of the live bundle -- each
floor's survey grid, planning grid, cloud sample and walked track -- by a
background job (maps/jobs.py), and kept as `quality.json` beside map.json.
PUT /maps/{id}/status refuses `active` unless the report is current and passed,
or a caller holding `override` says to freeze it anyway (maps/router.py).

"Current" means computed from exactly these files: `inputs` is a sha256 per
floor over its grid, planning grid, cloud and track, and the gate compares it
with the files on disk. Zones are deliberately not in it -- no check reads
them -- so drawing a zone does not make a reviewer re-run the report, while
replacing the bundle or a planning grid does.

THE CHECKS, per floor. `blocking` ones stop a freeze when they fail; the others
are warnings the console shows. A check that has nothing to measure (no cloud,
no track, no planning grid) is `skipped`, never `pass`.

  floor_tilt_deg      the floor plane fitted to the cloud sample (maps/
                      survey_math.fit_floor_plane, planning_grid.py's fit).
                      FAST-LIO2 saves the lidar's mount pitch into its frame
                      (4.8 deg on the 2026-11-09 office survey, 5.7 on the
                      2026-11-06 Venue); a levelled one is a fraction of a
                      degree. Over 1.0 deg (planning_grid.py TILT_WARN_DEG)
                      fails: heights, and so obstacles, are wrong across the map.
  track_tilt_deg      the same for the plane through the walked path, which on a
                      flat floor is parallel to it (scripts/survey/tilt.py).
  level_evidence      fails when there is neither a cloud nor a track: nothing
                      shows the map is level.
  floor_below_fraction  share of floor-fit cells clearly (> 8 cm) below the
                      plane. A plane with floor under it is not the floor --
                      or the floor is not one plane (a ramp, a step down). 5 %.
  floor_residual_p95_m  how far the floor's inlier cells sit from the plane.
                      Bounded by the fit's 8 cm inlier band, so information only.
  track_drift_p95_m   95th percentile of the walked path's height off its own
                      plane. The lidar rides at a fixed height, so on a flat
                      floor this is vertical drift. FAST-LIO2 has no loop
                      closure; this is the cheapest symptom of drift the upload
                      carries. Warns over 0.10 m.
  track_breaks        steps of the path over 1 m (planning_grid.py --max-step):
                      FAST-LIO2 losing track and snapping back. Warns on any.
  free_area_m2        walkable area: the planning grid's free cells when it has
                      one, else the survey grid's observed floor. Under 10 m2
                      there is nothing to plan a patrol on.
  surveyed_fraction   the survey grid's free + occupied share. Information
                      only: a lidar on a dog's back leaves most of a building
                      unknown (the office: ~2 %), and that is not a fault.
  track_inside_grid   share of path samples inside the grid. Under 99 % the
                      track and the grid are not in one frame (a raw track
                      with a levelled cloud, or a cropped grid).
  track_on_occupied   share of path samples on occupied survey-grid cells.
                      Information only: the person following the robot
                      leaves returns along the path, which is why
                      planning_grid.py clears the swept path.
  planning_grid       warns when the floor has none: nothing can plan on it.
  path_coverage       share of path samples on the planning grid's largest
                      free component (4-connected: a robot cannot pass two
                      cells that meet at a corner) -- planning_grid.py's own
                      acceptance figure, 95 % (--min-path-coverage).
  replay              matching recorded scans against the map
                      (roverhub-edge/robot_bridge/scan_locate.py). Always
                      `skipped` here: a survey upload carries the merged cloud
                      and the path, not the per-scan returns the matcher needs.
                      Matching synthetic scans cut from the same cloud would
                      only measure the cloud against itself. Building it needs
                      the bag's /scan (or per-scan clouds) in the upload.

Thresholds live in THRESHOLDS so the console can show them and a change is one
line. The office survey's numbers are in tests/test_maps_quality.py's notes.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from shared.schemas.maps import MapFloor, SiteMap

from .store import BundleError, MapNotFound, MapStore, parse_pgm

REPORT_VERSION = 1

# id -> (comparison, threshold, blocking). comparison "max": value <= threshold
# passes; "min": value >= threshold passes; "info": never fails.
THRESHOLDS: Dict[str, tuple] = {
    "level_evidence": ("present", None, True),
    "floor_tilt_deg": ("max", 1.0, True),
    "track_tilt_deg": ("max", 1.0, True),
    "floor_below_fraction": ("max", 0.05, True),
    "floor_residual_p95_m": ("info", None, False),
    "track_drift_p95_m": ("max", 0.10, False),
    "track_breaks": ("max", 0, False),
    "free_area_m2": ("min", 10.0, True),
    "surveyed_fraction": ("info", None, False),
    "track_inside_grid": ("min", 0.99, True),
    "track_on_occupied": ("info", None, False),
    "planning_grid": ("present", None, False),
    "path_coverage": ("min", 0.95, True),
    "replay": ("info", None, False),
}
MAX_STEP_M = 1.0
INPUT_KINDS = ("grid", "planning", "cloud", "track")
# Nav2's trinary read (store.OCCUPIED_THRESH / FREE_THRESH) as pixel values:
# a cell is occupied below 89.25 and free above 204.0 on a negate-0 PGM.
_OCCUPIED_BELOW = 255.0 * (1 - 0.65)
_FREE_ABOVE = 255.0 * (1 - 0.196)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def floor_inputs_digest(floor: MapFloor, files: Dict[str, Optional[bytes]]) -> str:
    """sha256 over what the checks read for one floor: its placement and the
    bytes of its grid, planning grid, cloud and track (an absent file counts
    as absent). Zones are not in it; see the module docstring."""
    digest = hashlib.sha256()
    digest.update(repr((floor.index, floor.resolution_m, tuple(floor.origin_xy_m),
                        tuple(floor.size_cells))).encode("ascii"))
    for kind in INPUT_KINDS:
        data = files.get(kind)
        digest.update(b"\0" + kind.encode("ascii") + b"\0")
        digest.update(b"<absent>" if data is None else data)
    return digest.hexdigest()


def inputs_digests(site_map: SiteMap, files: Dict[int, Dict[str, Optional[bytes]]]) -> Dict[str, str]:
    return {str(f.index): floor_inputs_digest(f, files.get(f.index, {})) for f in site_map.floors}


def is_current(store: MapStore, map_id: str, report: Optional[dict]) -> bool:
    """Whether `report` was computed from the files the map holds now."""
    if not isinstance(report, dict) or report.get("report_version") != REPORT_VERSION:
        return False
    try:
        site_map, version, files = store.snapshot(map_id, INPUT_KINDS)
    except MapNotFound:
        return False
    return report.get("version") == version and report.get("inputs") == inputs_digests(site_map, files)


def _check(check_id: str, floor: Optional[int], value, detail: str = "",
           status: Optional[str] = None) -> dict:
    comparison, threshold, blocking = THRESHOLDS[check_id]
    if status is None:
        if value is None:
            status = "skipped"
        elif comparison == "max":
            status = "pass" if value <= threshold else ("fail" if blocking else "warn")
        elif comparison == "min":
            status = "pass" if value >= threshold else ("fail" if blocking else "warn")
        else:
            status = "info"
    return {"id": check_id, "floor": floor, "value": value, "comparison": comparison,
            "threshold": threshold, "blocking": blocking, "status": status, "detail": detail}


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _floor_report(np, files: Dict[str, Optional[bytes]], floor: MapFloor) -> Tuple[dict, List[dict]]:
    from . import survey_math as sm

    index = floor.index
    width, height = floor.size_cells
    res = floor.resolution_m
    ox, oy = floor.origin_xy_m
    cell_m2 = res * res
    checks: List[dict] = []
    stats: Dict[str, object] = {"index": index}

    def read(kind: str) -> Optional[bytes]:
        return files.get(kind)

    def classes(pgm: bytes, label: str):
        w, h, pixels = parse_pgm(pgm)
        if (w, h) != (width, height):
            raise BundleError("floor %d's %s is %d x %d, not the floor's %d x %d" % (index, label, w, h, width, height))
        image = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width).astype(np.float64)
        return image < _OCCUPIED_BELOW, image > _FREE_ABOVE

    grid_occupied, grid_free = classes(read("grid") or b"", "grid")
    n_free, n_occ = int(grid_free.sum()), int(grid_occupied.sum())
    total = width * height
    stats["grid"] = {"cells": {"free": n_free, "occupied": n_occ, "unknown": total - n_free - n_occ},
                     "area_m2": {"free": n_free * cell_m2, "occupied": n_occ * cell_m2,
                                 "unknown": (total - n_free - n_occ) * cell_m2},
                     "surveyed_fraction": (n_free + n_occ) / total if total else 0.0}

    # --- the floor plane, from the cloud sample ---------------------------
    plane = None
    cloud = read("cloud")
    if cloud:
        xyz = np.frombuffer(cloud[: len(cloud) // 12 * 12], dtype="<f4").reshape(-1, 3).astype(np.float64)
        xyz = xyz[np.isfinite(xyz).all(axis=1)]
        plane = sm.fit_floor_plane(xyz) if len(xyz) >= 3 else None
        stats["cloud_points"] = int(len(xyz))
    stats["floor_plane"] = plane

    # --- the walked track --------------------------------------------------
    track = None
    raw_track = read("track")
    if raw_track:
        try:
            track = sm.read_track_csv(raw_track)
        except sm.SurveyError as exc:
            raise BundleError("floor %d's track: %s" % (index, exc)) from None
    track_info = None
    inside = rows = cols = None
    if track is not None:
        xy = track[:, 1:3]
        steps = np.hypot(*np.diff(xy, axis=0).T) if len(xy) > 1 else np.zeros(0)
        tp = sm.track_plane(track[:, 1:4])
        cols = np.floor((xy[:, 0] - ox) / res).astype(np.int64)
        j = np.floor((xy[:, 1] - oy) / res).astype(np.int64)
        inside = (cols >= 0) & (cols < width) & (j >= 0) & (j < height)
        rows = height - 1 - j
        on_occupied = np.zeros(len(xy), dtype=bool)
        on_occupied[inside] = grid_occupied[rows[inside], cols[inside]]
        track_info = {
            "samples": int(len(xy)),
            "samples_outside_grid": int((~inside).sum()),
            "length_m": float(steps[steps <= MAX_STEP_M].sum()),
            "breaks": int((steps > MAX_STEP_M).sum()),
            "jumped_m": float(steps[steps > MAX_STEP_M].sum()),
            "plane": tp,
            "inside_grid_fraction": float(inside.mean()),
            "on_occupied_fraction": float(on_occupied.mean()),
        }
    stats["track"] = track_info

    # --- the planning grid ---------------------------------------------------
    planning_info = None
    planning = read("planning")
    if planning:
        p_occupied, p_free = classes(planning, "planning grid")
        labels, count = sm.components(p_free, connectivity=4)
        sizes = np.bincount(labels.ravel(), minlength=count + 1)[1:] if count else np.zeros(0, int)
        largest = int(np.argmax(sizes)) + 1 if count else 0
        coverage = None
        if track is not None:
            on_largest = np.zeros(len(track), dtype=bool)
            if largest:
                on_largest[inside] = labels[rows[inside], cols[inside]] == largest
            coverage = float(on_largest.mean())
        pf, po = int(p_free.sum()), int(p_occupied.sum())
        planning_info = {
            "connectivity": 4,
            "cells": {"free": pf, "occupied": po, "unknown": total - pf - po},
            "area_m2": {"free": pf * cell_m2, "occupied": po * cell_m2, "unknown": (total - pf - po) * cell_m2},
            "free_components": int(count),
            "largest_component_cells": int(sizes[largest - 1]) if largest else 0,
            "largest_component_m2": float(sizes[largest - 1]) * cell_m2 if largest else 0.0,
            "path_coverage": coverage,
        }
    stats["planning"] = planning_info
    stats["replay"] = None

    # --- checks --------------------------------------------------------------
    checks.append(_check("level_evidence", index, plane is not None or track_info is not None,
                         "a floor plane from the cloud or a plane through the track" if (plane or track_info)
                         else "no cloud and no track: nothing shows this floor is level",
                         status="pass" if (plane is not None or (track_info and track_info["plane"])) else "fail"))
    checks.append(_check("floor_tilt_deg", index, _round(plane["tilt_deg"], 3) if plane else None,
                         "floor plane fitted to the cloud sample" if plane else
                         ("no cloud" if not cloud else "the cloud has too little floor to fit a plane")))
    tp = track_info["plane"] if track_info else None
    checks.append(_check("track_tilt_deg", index, _round(tp["tilt_deg"], 3) if tp else None,
                         "plane through the walked path" if tp else "no track" if track_info is None
                         else "the path is too straight to fit a plane"))
    checks.append(_check("floor_below_fraction", index, _round(plane["below_fraction"]) if plane else None,
                         "cells more than 8 cm below the fitted floor"))
    checks.append(_check("floor_residual_p95_m", index, _round(plane["residual_p95_m"]) if plane else None,
                         "floor cells' distance from the plane, 95th percentile"))
    checks.append(_check("track_drift_p95_m", index, _round(tp["residual_p95_m"]) if tp else None,
                         "path height off its own plane, 95th percentile: vertical drift"))
    checks.append(_check("track_breaks", index, track_info["breaks"] if track_info else None,
                         "path steps over %.1f m (tracking lost and snapped back)" % MAX_STEP_M))
    free_area = planning_info["area_m2"]["free"] if planning_info else stats["grid"]["area_m2"]["free"]
    checks.append(_check("free_area_m2", index, _round(free_area, 2),
                         "free area of the planning grid" if planning_info else
                         "observed floor in the survey grid (no planning grid)"))
    checks.append(_check("surveyed_fraction", index, _round(stats["grid"]["surveyed_fraction"]),
                         "free + occupied share of the survey grid"))
    checks.append(_check("track_inside_grid", index,
                         _round(track_info["inside_grid_fraction"]) if track_info else None,
                         "path samples inside the grid"))
    checks.append(_check("track_on_occupied", index,
                         _round(track_info["on_occupied_fraction"]) if track_info else None,
                         "path samples on occupied survey-grid cells"))
    checks.append(_check("planning_grid", index, planning_info is not None,
                         "a planning grid is stored" if planning_info else
                         "no planning grid: nothing can plan a walk on this floor",
                         status="pass" if planning_info else "warn"))
    checks.append(_check("path_coverage", index,
                         _round(planning_info["path_coverage"]) if planning_info else None,
                         "path samples on the planning grid's largest free area" if planning_info
                         else "no planning grid"))
    checks.append(_check("replay", index, None,
                         "not computed: the upload carries no recorded scans to match", status="skipped"))
    return stats, checks


def compute_report(store: MapStore, map_id: str) -> dict:
    """The report for the map's live bundle. Raises MapNotFound, or
    BundleError when a file does not fit its floor."""
    import numpy as np

    site_map, version, files = store.snapshot(map_id, INPUT_KINDS)
    floors, checks = [], []
    for floor in sorted(site_map.floors, key=lambda f: f.index):
        stats, floor_checks = _floor_report(np, files[floor.index], floor)
        floors.append(stats)
        checks.extend(floor_checks)
    if not site_map.floors:
        checks.append(_check("level_evidence", None, False, "the map has no floors", status="fail"))
    failed = sorted({c["id"] for c in checks if c["status"] == "fail"})
    report = {
        "report_version": REPORT_VERSION,
        "map_id": map_id,
        "version": version,
        "computed_at": _now(),
        "inputs": inputs_digests(site_map, files),
        "passed": not failed,
        "failed": failed,
        "warnings": sorted({c["id"] for c in checks if c["status"] == "warn"}),
        "thresholds": {k: {"comparison": v[0], "threshold": v[1], "blocking": v[2]}
                       for k, v in THRESHOLDS.items()},
        "checks": checks,
        "floors": floors,
    }
    return _finite(report)


def _finite(value):
    """JSON has no NaN or infinity: replace any with None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def run_quality(store: MapStore, map_id: str, progress=lambda stage: None) -> dict:
    """Compute and store the report (a job's work). Returns a summary."""
    progress("checking quality")
    try:
        version = store.current_version(map_id)
        report = compute_report(store, map_id)
    except MapNotFound:
        raise BundleError("map %s was deleted before it could be checked" % map_id) from None
    if report["version"] != version or not store.write_quality(map_id, report, expect_version=version):
        raise BundleError("the map's bundle was replaced while it was being checked; run the check again")
    return {"map_id": map_id, "version": version, "passed": report["passed"],
            "failed": report["failed"], "warnings": report["warnings"]}
