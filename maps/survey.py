"""Turn an uploaded survey (a .pcd and the path walked) into a draft map.

Tracker 1-5-02 (upload, backend half) and 1-5-04 (level the map on the
server). A background job (maps/jobs.py) runs `process_survey`:

  1. read the cloud (raw FAST-LIO2 output or already levelled) and the track;
  2. LEVEL it. FAST-LIO2's world frame is the lidar's pose at start-up, not
     gravity, so the mount pitch is saved into the map (4.84 deg on the
     2026-11-09 office survey). The floor is fitted as a plane
     (planning_grid.py's fit_floor_plane, via survey_math) and the cloud is
     rotated so that plane is horizontal (level_pcd.py's rotation). Where the
     cloud has too little floor to fit, the plane through the walked path is
     used instead, which on a flat floor is parallel to it (tilt.py);
  3. decide the TRACK's frame. A track exported with the raw cloud is tilted
     like it and is rotated with it; one exported after levelling is already
     flat and is left alone. `track_frame="auto"` tells them apart by the
     track's own plane: it agrees with the cloud's floor within
     TRACK_AGREES_DEG (raw), or is flat within LEVEL_TRACK_DEG (level), or the
     survey is refused with both numbers rather than guessed;
  4. build the floor's occupancy grid exactly as pcd_to_floorgrid.py does with
     its defaults (one floor, auto ground, default bands), from the points
     within trim.TRIM_MARGIN_M of the walked track only, and no wider than the
     track's bounds plus that margin (0.0.29: far stray returns stretched the
     2026-11-20 survey's canvas to 58 m; maps/trim.py);
  5. pack the floor as tools/map_prep/pack_map.py would, carry the map's other
     floors over unchanged, and store it with MapStore.save_bundle -- always a
     draft, the replaced bundle kept as a version, zones kept;
  6. run the quality report (maps/quality.py) on what was stored.

What is kept: the grid, its preview, a 200k-point sample of the floor's cloud
(the console's 3D view, as pcd_to_floorgrid.py writes it), the LEVELLED track,
the "probably moved" layer when one was sent (moved.json and moved.pcd from
tools/map_prep/transient_filter.py, IN THE UPLOADED CLOUD'S FRAME: rotated
with the cloud when it is levelled here, see cloud_edit.rotate_moved), and survey.json (the rotation applied, both planes before and after, and the
grid parameters) -- so the levelling can be reproduced on the full cloud
locally, e.g. for planning_grid.py. The full cloud itself is not kept.

NOT DONE: LOOP-CLOSURE DRIFT CORRECTION (tracker 1-5-04, second half).
Assessed 2026-11-12 and not built, because the upload cannot support it.
Correcting drift means re-estimating the trajectory with loop constraints --
recognising that the robot is back somewhere it has been, registering the
scans taken there against each other, and optimising the pose graph -- and
then re-assembling the map from each scan at its corrected pose. All of that
needs the individual scans with their timestamps (the rosbag's /cloud_registered
or /livox/lidar), which a merged .pcd has thrown away: once FAST-LIO2 has
summed the scans, a doubled wall cannot be un-summed. It also needs a
registration and pose-graph library (small_gicp / GTSAM, or a SLAM backend with
loop closure such as GLIM or FAST-LIO-SAM) that the backend should not carry.
The place for it is the survey device or an offline tool, upstream of this
upload. What the server can do is notice drift: quality.py reports the path's
vertical drift and any tracking breaks.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from .store import (
    ARTIFACT_FILES, BUNDLE_FILE, MAX_CLOUD_BYTES, MAX_FLOOR_LABEL_CHARS, MAX_MOVED_BYTES, BundleError, MapStore,
    _link_or_copy, count_cells, valid_map_id,
)
from .trim import TRIM_MARGIN_M, near_track, track_bounds

# Under this the cloud is already level and is left alone: about the floor
# fit's own precision, and 5 cm of height across a 30 m building.
LEVEL_BELOW_DEG = 0.1
# The raw cloud is tilted by the lidar's mount (FAST-LIO2's frame is the IMU's
# at start). The schema allows a mount pitch up to 45 deg (shared
# sensors.LidarMount); rover-jetson's has been 19.8 deg since its 2026-11-18
# remount, and the old 15 deg cap refused every survey after it. A floor
# plane steeper than a mount can be is a ramp, a wall, or a fit gone wrong
# (11-18's runaway survey fitted 70 deg) -- not something to rotate away.
MAX_LEVEL_DEG = 45.0
# The track's plane and the cloud's floor plane agree within this when the
# track is in the cloud's (raw) frame.
TRACK_AGREES_DEG = 1.0
# A track flatter than this is taken to be already levelled.
LEVEL_TRACK_DEG = 1.0
TRACK_FRAMES = ("auto", "raw", "level")
# StartSurveyParams.survey_id's bounds (shared/schemas/survey.py).
MAX_SURVEY_ID_CHARS = 64
WEB_CLOUD_POINTS = 200_000


@dataclass
class SurveyRequest:
    map_id: str
    site_id: str
    name: str
    floor_index: int = 0
    resolution_m: float = 0.05
    track_frame: str = "auto"
    cloud_name: str = "cloud.pcd"
    track_name: str = "track.csv"
    # Which console survey made this (StartSurveyParams.survey_id) and on which
    # robot, when known: kept in survey.json so the map editor's "Find moving
    # things again" (FIND_MOVING) can name the kept recording and the robot
    # holding it, through restores and versions (schema 0.0.23).
    survey_id: Optional[str] = None
    robot_id: Optional[str] = None
    # The floor's name, e.g. "2F" (StartSurveyParams.floor_label, 0.0.29),
    # stored as its label. None keeps the label a re-surveyed floor already has.
    floor_label: Optional[str] = None

    def check(self) -> None:
        from .survey_math import SurveyError

        if not valid_map_id(self.map_id):
            raise SurveyError("map_id %r is not a valid map id" % (self.map_id,))
        if not isinstance(self.site_id, str) or not self.site_id.strip():
            raise SurveyError("site_id is required")
        if not isinstance(self.name, str) or not self.name.strip():
            raise SurveyError("name is required")
        if not 0 <= self.floor_index <= 999:
            raise SurveyError("floor must be between 0 and 999")
        if not (math.isfinite(self.resolution_m) and 0.01 <= self.resolution_m <= 0.5):
            raise SurveyError("resolution_m must be between 0.01 and 0.5 m")
        if self.track_frame not in TRACK_FRAMES:
            raise SurveyError("track_frame must be one of %s" % ", ".join(TRACK_FRAMES))
        if self.survey_id is not None and not (
                isinstance(self.survey_id, str) and 1 <= len(self.survey_id) <= MAX_SURVEY_ID_CHARS):
            raise SurveyError("survey_id must be 1 to %d characters" % MAX_SURVEY_ID_CHARS)
        if self.floor_label is not None and not (
                isinstance(self.floor_label, str) and len(self.floor_label) <= MAX_FLOOR_LABEL_CHARS):
            raise SurveyError("floor_label must be at most %d characters" % MAX_FLOOR_LABEL_CHARS)


def slug(text: str) -> str:
    """pcd_to_floorgrid.py slug: a map id from a name."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "map"


def _slope_angle_deg(a: float, b: float) -> float:
    return math.degrees(math.atan(math.hypot(a, b)))


def level_survey(xyz, track, track_frame: str = "auto") -> Dict:
    """Level the cloud (and the track, when it is in the cloud's frame).

    Returns {"cloud": levelled xyz, "track": levelled (N, 4) track, "info":
    what was measured and done}. Raises SurveyError when neither the cloud nor
    the track can say where the floor is, when the tilt is beyond a mount
    pitch, or when the track's frame cannot be told."""
    import numpy as np

    from . import survey_math as sm

    cloud_plane = sm.fit_floor_plane(xyz)
    track_plane = sm.track_plane(track[:, 1:4])
    if cloud_plane is not None:
        a, b, basis = cloud_plane["a"], cloud_plane["b"], "cloud floor plane"
    elif track_plane is not None and track_frame != "level":
        a, b, basis = track_plane["a"], track_plane["b"], "track plane (the cloud had too little floor)"
    else:
        raise sm.SurveyError("cannot find the floor: the cloud has too little floor to fit a plane, "
                             "and the track cannot stand in for it")
    rotation, angle = sm.levelling_rotation(a, b)
    if angle > MAX_LEVEL_DEG:
        raise sm.SurveyError("the floor is tilted %.1f deg, more than a lidar mount can be (%.0f deg), so "
                             "this is a ramp, a wall or a bad fit, and it is not rotated away"
                             % (angle, MAX_LEVEL_DEG))
    rotate = angle >= LEVEL_BELOW_DEG

    decided = track_frame
    reason = "as stated"
    if track_frame == "auto":
        if track_plane is None:
            decided, reason = "raw", "the path is too straight to fit a plane; assumed in the cloud's frame"
        else:
            disagreement = _slope_angle_deg(track_plane["a"] - a, track_plane["b"] - b)
            if disagreement <= TRACK_AGREES_DEG:
                decided = "raw"
                reason = "its plane agrees with the cloud's floor within %.2f deg" % disagreement
            elif track_plane["tilt_deg"] <= LEVEL_TRACK_DEG:
                decided = "level"
                reason = "it is flat (%.2f deg) while the cloud is tilted %.2f deg" % (
                    track_plane["tilt_deg"], angle)
            else:
                raise sm.SurveyError(
                    "the track is tilted %.2f deg and the cloud's floor %.2f deg, %.2f deg apart: the track "
                    "is neither in the cloud's frame nor level. Export them from the same run, or say "
                    "track_frame=raw or level" % (track_plane["tilt_deg"], angle, disagreement))

    levelled = xyz @ rotation.T if rotate else xyz
    track_levelled = track.copy()
    if rotate and decided == "raw":
        track_levelled[:, 1:4] = track[:, 1:4] @ rotation.T
    after = sm.fit_floor_plane(levelled) if rotate else cloud_plane
    track_after = sm.track_plane(track_levelled[:, 1:4])
    info = {
        "basis": basis,
        "rotated": rotate,
        "angle_deg": angle,
        "rotation": np.round(rotation, 12).tolist(),
        "cloud_plane_before": cloud_plane,
        "cloud_plane_after": after,
        "track_frame": decided,
        "track_frame_reason": reason,
        "track_plane_before": track_plane,
        "track_plane_after": track_after,
    }
    return {"cloud": levelled, "track": track_levelled, "info": info}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_survey(store: MapStore, request: SurveyRequest, cloud_path: Path, track_bytes: bytes,
                   work_dir: Path, progress: Callable[[str], None] = lambda stage: None,
                   detail: Optional[dict] = None, moved_path: Optional[Path] = None,
                   moved_cloud_path: Optional[Path] = None) -> dict:
    """Level, grid, pack and store the survey as a draft; then check its
    quality. Returns the job result. Raises SurveyError / BundleError (shown
    to the uploader) for a survey that cannot be made into a map."""
    from . import survey_math as sm
    from .quality import run_quality

    import numpy as np

    from .cloud_edit import moved_bytes, parse_moved, pcd_bytes, read_pcd_bytes, rotate_moved

    request.check()
    # The moved layer is checked before the long work, so a bad one fails fast.
    moved = moved_points = None
    if moved_path is not None:
        if moved_path.stat().st_size > MAX_MOVED_BYTES:
            raise BundleError("the moved layer exceeds %d bytes" % MAX_MOVED_BYTES)
        moved = parse_moved(moved_path.read_bytes())
    if moved_cloud_path is not None:
        if moved_cloud_path.stat().st_size > MAX_CLOUD_BYTES:
            raise BundleError("the moved cloud exceeds %d bytes" % MAX_CLOUD_BYTES)
        moved_points = read_pcd_bytes(moved_cloud_path.read_bytes(), MAX_CLOUD_BYTES // 12)
    progress("reading the cloud")
    xyz = sm.read_pcd_xyz(cloud_path)
    track = sm.read_track_csv(track_bytes)
    points = int(len(xyz))

    progress("levelling")
    level = level_survey(xyz, track, request.track_frame)
    del xyz

    progress("building the grid")
    # Only what lies near the walked path makes the floor (maps/trim.py): a
    # few far returns -- through a window, along a corridor only looked down
    # -- stretched the 2026-11-20 15:08 survey's canvas to 58 m for an 11 m
    # office. The robot's recording keeps them.
    track_xy = level["track"][:, 1:3]
    near = near_track(level["cloud"][:, :2], track_xy, TRIM_MARGIN_M)
    beyond_margin = int(len(near) - int(near.sum()))
    if beyond_margin:
        level["cloud"] = level["cloud"][near]
    del near
    if not len(level["cloud"]):
        raise sm.SurveyError("no part of the cloud lies within %.0f m of the walked track" % TRIM_MARGIN_M)
    grid = sm.floor_grid(level["cloud"], resolution=request.resolution_m,
                         within=track_bounds(track_xy, TRIM_MARGIN_M))
    image = grid["image"]
    rows, cols = image.shape
    free, occupied, unknown = count_cells(image.tobytes())
    cloud_bytes, cloud_points = sm.web_cloud_bytes(grid["floor_cloud"], WEB_CLOUD_POINTS)
    if len(cloud_bytes) > MAX_CLOUD_BYTES:  # 200k points is 2.4 MB; a guard, not a limit in use
        cloud_bytes, cloud_points = sm.web_cloud_bytes(grid["floor_cloud"], MAX_CLOUD_BYTES // 12)
    del level["cloud"]

    progress("storing")
    bundle = work_dir / "bundle"
    floor_dir = bundle / ("floor%d" % request.floor_index)
    floor_dir.mkdir(parents=True)
    (floor_dir / ARTIFACT_FILES["grid"]).write_bytes(sm.pgm_bytes(image))
    (floor_dir / ARTIFACT_FILES["preview"]).write_bytes(sm.png_bytes(image))
    (floor_dir / ARTIFACT_FILES["cloud"]).write_bytes(cloud_bytes)
    (floor_dir / ARTIFACT_FILES["track"]).write_bytes(sm.track_csv_bytes(level["track"]))
    info = level["info"]
    if moved is not None:
        if info["rotated"]:
            moved = rotate_moved(moved, info["rotation"], info["angle_deg"])
        (floor_dir / ARTIFACT_FILES["moved"]).write_bytes(moved_bytes(moved))
    if moved_points is not None:
        if info["rotated"]:
            moved_points = moved_points @ np.asarray(info["rotation"]).T
        (floor_dir / ARTIFACT_FILES["moved_cloud"]).write_bytes(pcd_bytes(moved_points))
    origin = [round(grid["origin"][0], 4), round(grid["origin"][1], 4)]
    survey_doc = {
        "generated_by": "roverhub-backend maps/survey.py",
        "generated_at": _now(),
        "source": {"cloud": request.cloud_name, "points": points,
                   "track": request.track_name, "track_samples": int(len(track))},
        "levelling": level["info"],
        "grid": {"resolution_m": request.resolution_m, "floor_z_m": grid["floor_z"],
                 "ceiling_z_m": grid["ceiling_z"], "obstacle_band_m": list(grid["obstacle_band"]),
                 "ground_band_m": list(grid["ground_band"]), "min_hits": grid["min_hits"],
                 "min_ground_hits": grid["min_ground_hits"],
                 "trim_margin_m": TRIM_MARGIN_M, "points_beyond_margin": beyond_margin,
                 "method": "tools/map_prep/pcd_to_floorgrid.py defaults (observed ground, no ray casting)"},
        "cloud_sample_points": cloud_points,
        # Which console survey and robot made it; nulls for an upload by hand.
        "survey": {"survey_id": request.survey_id, "robot_id": request.robot_id},
    }
    (floor_dir / ARTIFACT_FILES["survey"]).write_text(json.dumps(sm_safe(survey_doc), indent=2), encoding="utf-8")
    floors = [{
        "index": request.floor_index,
        "floor_z_m": round(grid["floor_z"], 3),
        "resolution_m": request.resolution_m,
        "origin_xy_m": origin,
        "size_cells": [cols, rows],
        "free_cells": free, "occupied_cells": occupied, "unknown_cells": unknown,
    }]
    extent_min = [origin[0], origin[1]]
    extent_max = [round(origin[0] + cols * request.resolution_m, 4),
                  round(origin[1] + rows * request.resolution_m, 4)]

    # The map's other floors come along unchanged (their own planning grids too).
    existing = store.get(request.map_id)
    created_at = _now()
    if request.floor_label is not None:
        floors[0]["label"] = request.floor_label
    if existing is not None:
        for other in existing.floors:
            if other.index == request.floor_index:
                if request.floor_label is None and other.label is not None:
                    floors[0]["label"] = other.label
                continue
            source = store.root / request.map_id / ("floor%d" % other.index)
            target = bundle / ("floor%d" % other.index)
            target.mkdir()
            for file_name in ARTIFACT_FILES.values():
                if (source / file_name).is_file():
                    _link_or_copy(source / file_name, target / file_name)
            floors.append(other.model_dump(mode="json", exclude={"content_hash"}))
            width, height = other.size_cells
            extent_min = [min(extent_min[0], other.origin_xy_m[0]), min(extent_min[1], other.origin_xy_m[1])]
            extent_max = [max(extent_max[0], other.origin_xy_m[0] + width * other.resolution_m),
                          max(extent_max[1], other.origin_xy_m[1] + height * other.resolution_m)]
    floors.sort(key=lambda f: f["index"])
    (bundle / BUNDLE_FILE).write_text(json.dumps({
        "site_map": {"map_id": request.map_id, "site_id": request.site_id, "name": request.name,
                     "source": "slam", "extent_min": extent_min, "extent_max": extent_max,
                     "created_at": created_at, "updated_at": created_at},
        "floors": floors,
        "provenance": {"generated_by": "roverhub-backend maps/survey.py", "generated_at": created_at},
    }, indent=2), encoding="utf-8")
    saved = store.save_bundle(bundle, source="survey", detail={
        "floor": request.floor_index, "cloud": request.cloud_name, "points": points,
        "levelled_deg": round(level["info"]["angle_deg"], 3) if level["info"]["rotated"] else 0.0,
        **(detail or {})})
    version = store.current_version(saved.map_id)

    quality = run_quality(store, saved.map_id, progress)
    return {"map_id": saved.map_id, "version": version, "floor": request.floor_index,
            "points": points, "levelled": info["rotated"], "levelled_deg": round(info["angle_deg"], 3),
            "track_frame": info["track_frame"],
            "tilt_after_deg": round(info["cloud_plane_after"]["tilt_deg"], 3) if info["cloud_plane_after"] else None,
            "size_cells": [cols, rows], "free_cells": free, "occupied_cells": occupied,
            "moved_blobs": None if moved is None else len(moved["blobs"]),
            "quality": quality}


def sm_safe(value):
    """JSON-safe copy: numpy scalars to Python, non-finite floats to None."""
    if isinstance(value, dict):
        return {k: sm_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sm_safe(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
