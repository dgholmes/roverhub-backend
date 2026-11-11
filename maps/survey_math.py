"""The numeric half of turning a survey into a map, vendored from tools/map_prep.

The root repo's tools/map_prep/ scripts (pcd_to_floorgrid.py, planning_grid.py)
and roverhub-edge/scripts/survey/level_pcd.py are the reference
implementations. They are command-line scripts that import each other by bare
module name from a different repository, so the backend cannot import them
cleanly: it would need that checkout on sys.path at a fixed relative location,
which no deployment of this service has. The minimum the server needs is copied
here instead, each function naming the one it was copied from. Keep them in
step: a fix to the floor fit there is a fix here.

What differs from the sources, on purpose:

- errors raise SurveyError (a ValueError whose message is safe to show the
  uploader) instead of SystemExit, and nothing prints;
- inputs are bounded (MAX_POINTS) because the file came over HTTP;
- the writers return bytes rather than writing paths.

numpy only, like the sources: no PIL, no scipy.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# Nav2's trinary greys (pcd_to_floorgrid.py PGM_FREE / PGM_UNKNOWN / PGM_OCCUPIED).
PGM_FREE = 254
PGM_UNKNOWN = 205
PGM_OCCUPIED = 0

# A survey is bounded by what the server will hold in memory at once. The
# 2026-11-09 office survey is 2.76 M points; 30 M (about 2 GB of float64 work
# arrays at peak) is a building ten times that.
MAX_POINTS = 30_000_000

# planning_grid.py's floor-plane fit parameters (PLANE_*), unchanged.
PLANE_CELL_M = 0.5
PLANE_PERCENTILE = 2.0
PLANE_INLIER_M = 0.08
PLANE_MIN_CELLS = 20
PLANE_TRIES = 2000


class SurveyError(ValueError):
    """The survey cannot be processed. The message is safe to show the uploader."""


# --------------------------------------------------------------------------
# reading -- pcd_to_floorgrid.py read_pcd_header / read_xyz
# --------------------------------------------------------------------------

def _pcd_header(path: Path) -> Tuple[Dict[str, List[str]], int, int]:
    """(fields, byte offset of the data, number of header lines)."""
    fields: Dict[str, List[str]] = {}
    offset = 0
    with path.open("rb") as handle:
        for lines in range(1, 65):  # a PCD header is about a dozen lines
            raw = handle.readline(4096)
            if not raw:
                raise SurveyError("the cloud ends before its DATA line; is it a .pcd?")
            offset += len(raw)
            line = raw.decode("ascii", "replace").strip()
            if not line or line.startswith("#"):
                continue
            key, _, rest = line.partition(" ")
            fields[key.upper()] = rest.split()
            if key.upper() == "DATA":
                return fields, offset, lines
    raise SurveyError("the cloud has no DATA line in its first 64 lines; is it a .pcd?")


def read_pcd_xyz(path: Path, max_points: int = MAX_POINTS) -> "np.ndarray":
    """(N, 3) float64 finite points of a PCD (ascii or binary). Copied from
    pcd_to_floorgrid.py read_xyz."""
    fields, offset, header_lines = _pcd_header(path)
    try:
        names = fields["FIELDS"]
        sizes = [int(v) for v in fields["SIZE"]]
        types = [t.upper() for t in fields["TYPE"]]
        counts = [int(v) for v in fields.get("COUNT", ["1"] * len(names))]
        points = int(fields["POINTS"][0]) if "POINTS" in fields else int(fields["WIDTH"][0])
        data = fields["DATA"][0].lower()
    except (KeyError, IndexError, ValueError):
        raise SurveyError("the cloud's PCD header is missing FIELDS, SIZE, TYPE or POINTS") from None
    if not {"x", "y", "z"} <= set(names):
        raise SurveyError("the cloud has no x, y, z fields (it has %s)" % ", ".join(names))
    if not 0 < points <= max_points:
        raise SurveyError("the cloud says it holds %d points; this server takes 1 to %d"
                          % (points, max_points))
    if data == "binary_compressed":
        raise SurveyError("the cloud is DATA binary_compressed (LZF); re-save it uncompressed, "
                          "e.g. pcl_convert_pcd_ascii_binary in.pcd out.pcd 1")
    if data == "ascii":
        column = {name: i for i, name in enumerate(names)}
        try:
            raw = np.loadtxt(path, comments="#", skiprows=header_lines, usecols=(column["x"], column["y"], column["z"]),
                             dtype=np.float64, max_rows=points, ndmin=2)
        except ValueError as exc:
            raise SurveyError("the cloud's ascii data does not parse: %s" % exc) from None
        xyz = raw
    elif data == "binary":
        kind = {"F": "f", "U": "u", "I": "i"}
        parts = []
        if not len(names) == len(sizes) == len(types) == len(counts):
            raise SurveyError("the cloud's FIELDS, SIZE, TYPE and COUNT lines differ in length")
        for name, size, type_, count in zip(names, sizes, types, counts, strict=True):
            if count != 1 or type_ not in kind or size not in (1, 2, 4, 8):
                raise SurveyError("field %r is %s%d x %d; only single scalar fields are handled"
                                  % (name, type_, size, count))
            parts.append((name, "<" + kind[type_] + str(size)))
        dtype = np.dtype(parts)
        if path.stat().st_size - offset < dtype.itemsize * points:
            raise SurveyError("the cloud is shorter than its header says (%d points of %d bytes)"
                              % (points, dtype.itemsize))
        array = np.fromfile(path, dtype=dtype, count=points, offset=offset)
        xyz = np.stack([array["x"], array["y"], array["z"]], axis=1).astype(np.float64)
    else:
        raise SurveyError("the cloud's DATA is %r; ascii or binary is handled" % data)
    xyz = xyz[np.isfinite(xyz).all(axis=1)]
    if len(xyz) < 3:
        raise SurveyError("the cloud has fewer than 3 finite points")
    return xyz


def read_track_csv(data: bytes, max_rows: int = 2_000_000) -> "np.ndarray":
    """(N, 4) t, x, y, z from a CSV whose first line is t,x,y,z. Copied from
    planning_grid.py read_track."""
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise SurveyError("the track is not UTF-8 text") from None
    if not lines or [h.strip().lower() for h in lines[0].split(",")] != ["t", "x", "y", "z"]:
        raise SurveyError("the track's first line must be the header t,x,y,z")
    if len(lines) - 1 > max_rows:
        raise SurveyError("the track has more than %d rows" % max_rows)
    rows = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        try:
            values = [float(p) for p in line.split(",")]
        except ValueError:
            raise SurveyError("track line %d is not four numbers" % number) from None
        if len(values) != 4 or not all(math.isfinite(v) for v in values):
            raise SurveyError("track line %d is not four finite numbers" % number)
        rows.append(values)
    if not rows:
        raise SurveyError("the track has no samples")
    track = np.asarray(rows, dtype=np.float64)
    if (np.diff(track[:, 0]) < 0).any():
        first = int(np.flatnonzero(np.diff(track[:, 0]) < 0)[0]) + 3
        raise SurveyError("track line %d: time goes backwards; the track must be sorted by t" % first)
    return track


def track_csv_bytes(track: "np.ndarray") -> bytes:
    return ("t,x,y,z\n" + "".join("%.3f,%.4f,%.4f,%.4f\n" % tuple(row) for row in track)).encode("ascii")


# --------------------------------------------------------------------------
# the floor -- planning_grid.py fit_floor_plane / track_tilt_deg,
# pcd_to_floorgrid.py _cell_keys / find_ground / find_ceiling / trim_bounds
# --------------------------------------------------------------------------

def _cell_keys(xyz: "np.ndarray", cell_m: float) -> Tuple["np.ndarray", int, int, float, float]:
    x0, y0 = float(xyz[:, 0].min()), float(xyz[:, 1].min())
    cols = max(1, int((xyz[:, 0].max() - x0) / cell_m) + 1)
    rows = max(1, int((xyz[:, 1].max() - y0) / cell_m) + 1)
    col = np.clip(((xyz[:, 0] - x0) / cell_m).astype(np.int64), 0, cols - 1)
    row = np.clip(((xyz[:, 1] - y0) / cell_m).astype(np.int64), 0, rows - 1)
    return row * cols + col, rows, cols, x0, y0


def fit_floor_plane(xyz: "np.ndarray", cell_m: float = PLANE_CELL_M,
                    percentile: float = PLANE_PERCENTILE, inlier_m: float = PLANE_INLIER_M,
                    min_points_per_cell: int = 5, min_cells: int = PLANE_MIN_CELLS,
                    tries: int = PLANE_TRIES, seed: int = 0) -> Optional[Dict]:
    """The floor as a plane z = a*x + b*y + c, or None if the cloud cannot say.

    Copied from planning_grid.py fit_floor_plane (see its docstring for why a
    low percentile per cell, and why RANSAC penalises cells below the plane).
    Adds `below_fraction` and `inlier_fraction` for the quality report.
    """
    if len(xyz) < 3:
        return None
    key, _, _, _, _ = _cell_keys(xyz, cell_m)
    order = np.lexsort((xyz[:, 2], key))
    keys = key[order]
    starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
    counts = np.diff(np.r_[starts, len(keys)])
    keep = counts >= min_points_per_cell
    if int(keep.sum()) < min_cells:
        return None
    chosen = xyz[order[starts + np.floor(percentile / 100.0 * (counts - 1)).astype(np.int64)]][keep]
    low = chosen[:, 2]
    A = np.column_stack([chosen[:, 0], chosen[:, 1], np.ones(len(chosen))])

    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(low), size=(tries, 3))
    triples = A[picks]
    picks = picks[np.abs(np.linalg.det(triples)) > 1e-6]
    if picks.size == 0:
        return None
    best, best_score = None, -np.inf
    for start in range(0, len(picks), 250):
        chunk = picks[start:start + 250]
        planes = np.linalg.solve(A[chunk], low[chunk][..., None])[..., 0]
        residual = low[None, :] - planes @ A.T
        score = ((np.abs(residual) < inlier_m).sum(axis=1)
                 - (residual < -inlier_m).sum(axis=1))
        index = int(np.argmax(score))
        if score[index] > best_score:
            best, best_score = planes[index], score[index]

    plane = best
    for _ in range(10):
        inliers = np.abs(low - A @ plane) < inlier_m
        if inliers.sum() < min_cells or np.linalg.matrix_rank(A[inliers]) < 3:
            return None
        plane, *_ = np.linalg.lstsq(A[inliers], low[inliers], rcond=None)
    residual = low - A @ plane
    inliers = np.abs(residual) < inlier_m
    if inliers.sum() < min_cells:
        return None
    a, b, c = (float(v) for v in plane)
    below = int((residual < -inlier_m).sum())
    return {"a": a, "b": b, "c": c,
            "tilt_deg": math.degrees(math.atan(math.hypot(a, b))),
            "inlier_cells": int(inliers.sum()), "cells": int(len(low)),
            "below_cells": below,
            "inlier_fraction": float(inliers.sum()) / len(low),
            "below_fraction": below / len(low),
            "residual_p95_m": float(np.percentile(np.abs(residual[inliers]), 95))}


def track_plane(xyz: "np.ndarray") -> Optional[Dict]:
    """The plane best fitting the sensor's path, with its residuals.

    planning_grid.py track_tilt_deg's fit (centred, so a straight path does not
    invent a slope), plus the residual spread roverhub-edge/scripts/survey/tilt.py
    prints: on a flat floor the path's height above the plane should be
    constant, so its spread is vertical drift. None for under 3 samples or a
    path with no extent in one direction (a straight line has no plane)."""
    if len(xyz) < 3:
        return None
    centre = xyz.mean(axis=0)
    centred = xyz - centre
    if np.linalg.matrix_rank(centred[:, :2], tol=1e-6) < 2:
        return None
    (a, b), *_ = np.linalg.lstsq(centred[:, :2], centred[:, 2], rcond=None)
    residual = centred[:, 2] - centred[:, :2] @ np.array([a, b])
    return {"a": float(a), "b": float(b), "c": float(centre[2] - a * centre[0] - b * centre[1]),
            "tilt_deg": math.degrees(math.atan(math.hypot(a, b))),
            "residual_std_m": float(residual.std()),
            "residual_p95_m": float(np.percentile(np.abs(residual), 95)),
            "residual_max_m": float(np.abs(residual).max())}


def levelling_rotation(a: float, b: float) -> Tuple["np.ndarray", float]:
    """(R, degrees): the rotation taking the plane z = a*x + b*y + c's normal
    onto +z, by the smallest angle (Rodrigues). Copied from
    roverhub-edge/scripts/survey/level_pcd.py. Apply as xyz @ R.T."""
    n = np.array([-a, -b, 1.0])
    n /= np.linalg.norm(n)
    ez = np.array([0.0, 0.0, 1.0])
    axis = np.cross(n, ez)
    s = float(np.linalg.norm(axis))
    c = float(n @ ez)
    if s < 1e-12:
        return np.eye(3), 0.0
    k = axis / s
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    theta = math.atan2(s, c)
    R = np.eye(3) + math.sin(theta) * K + (1 - math.cos(theta)) * (K @ K)
    return R, math.degrees(theta)


def find_ground(xyz: "np.ndarray", cell_m: float = 0.5, percentile: float = 10.0,
                min_points_per_cell: int = 5) -> float:
    """pcd_to_floorgrid.py find_ground: a low percentile of each cell's lowest return."""
    key, _, _, _, _ = _cell_keys(xyz, cell_m)
    size = int(key.max()) + 1
    lowest = np.full(size, np.inf)
    np.minimum.at(lowest, key, xyz[:, 2])
    populated = np.bincount(key, minlength=size) >= min_points_per_cell
    lowest = lowest[populated & np.isfinite(lowest)]
    if lowest.size == 0:
        return float(np.percentile(xyz[:, 2], 2))
    return float(np.percentile(lowest, percentile))


def find_ceiling(xyz: "np.ndarray", cell_m: float = 0.5, percentile: float = 50.0,
                 min_points_per_cell: int = 5) -> float:
    """pcd_to_floorgrid.py find_ceiling: a percentile of each cell's highest return."""
    key, _, _, _, _ = _cell_keys(xyz, cell_m)
    size = int(key.max()) + 1
    highest = np.full(size, -np.inf)
    np.maximum.at(highest, key, xyz[:, 2])
    populated = np.bincount(key, minlength=size) >= min_points_per_cell
    highest = highest[populated & np.isfinite(highest)]
    if highest.size == 0:
        return float(np.percentile(xyz[:, 2], 98))
    return float(np.percentile(highest, percentile))


def trim_bounds(xyz: "np.ndarray", cell_m: float = 1.0, drop_percent: float = 0.1,
                margin_m: float = 1.0) -> Tuple[Tuple[float, float, float, float], int]:
    """pcd_to_floorgrid.py trim_bounds: the extent of the cells holding real
    structure, dropping at most `drop_percent` % of returns, sparsest cells first."""
    if drop_percent <= 0:
        return (float(xyz[:, 0].min()), float(xyz[:, 1].min()),
                float(xyz[:, 0].max()), float(xyz[:, 1].max())), 0
    key, _, cols, x0, y0 = _cell_keys(xyz, cell_m)
    counts = np.bincount(key)
    occupied = np.flatnonzero(counts)
    budget = drop_percent / 100.0 * float(len(xyz))
    order = occupied[np.argsort(counts[occupied], kind="stable")]
    cumulative = np.cumsum(counts[order])
    droppable = int(np.searchsorted(cumulative, budget, side="right"))
    kept = order[droppable:]
    if kept.size == 0:
        return (float(xyz[:, 0].min()), float(xyz[:, 1].min()),
                float(xyz[:, 0].max()), float(xyz[:, 1].max())), 0
    kept_cols, kept_rows = kept % cols, kept // cols
    x_lo = x0 + float(kept_cols.min()) * cell_m - margin_m
    x_hi = x0 + float(kept_cols.max() + 1) * cell_m + margin_m
    y_lo = y0 + float(kept_rows.min()) * cell_m - margin_m
    y_hi = y0 + float(kept_rows.max() + 1) * cell_m + margin_m
    outside = ((xyz[:, 0] < x_lo) | (xyz[:, 0] > x_hi)
               | (xyz[:, 1] < y_lo) | (xyz[:, 1] > y_hi))
    return (x_lo, y_lo, x_hi, y_hi), int(outside.sum())


# --------------------------------------------------------------------------
# the grid -- pcd_to_floorgrid.py rasterise and its default bands
# --------------------------------------------------------------------------

def floor_grid(xyz: "np.ndarray", resolution: float = 0.05, min_hits: int = 6,
               min_ground_hits: int = 2, ground_band: Tuple[float, float] = (-0.18, 0.18),
               max_cells: int = 64_000_000) -> Dict:
    """One floor's occupancy grid from a LEVEL cloud, as pcd_to_floorgrid.py
    main() makes it with its defaults (--floors auto, the default obstacle band,
    --min-hits 6, --min-ground-hits 2, --trim-drop 0.1). Returns the image
    (rows, cols) uint8 with row 0 at max y, its origin, the floor and ceiling
    heights, the bands used, and the trimmed cloud of this floor."""
    bounds, _ = trim_bounds(xyz)
    x_min, y_min, x_max, y_max = bounds
    xyz = xyz[(xyz[:, 0] >= x_min) & (xyz[:, 0] <= x_max)
              & (xyz[:, 1] >= y_min) & (xyz[:, 1] <= y_max)]
    z = xyz[:, 2]
    ground = find_ground(xyz)
    ceiling = find_ceiling(xyz)
    indoor = 1.5 <= (ceiling - ground) <= 8.0
    top = min(ceiling - 0.30, ground + 2.00) if indoor else ground + 2.00
    obstacle_band = (0.15, round(top - ground, 2))
    floor_span = (ceiling - ground) if indoor else 2.6

    cols = max(1, int(math.ceil((x_max - x_min) / resolution)))
    rows = max(1, int(math.ceil((y_max - y_min) / resolution)))
    if rows * cols > max_cells:
        raise SurveyError("the floor would be %d x %d cells at %.3f m; that is more than %d"
                          % (cols, rows, resolution, max_cells))

    def accumulate(points: "np.ndarray") -> "np.ndarray":
        hits = np.zeros(rows * cols, dtype=np.int32)
        if points.size == 0:
            return hits.reshape(rows, cols)
        col = np.clip(((points[:, 0] - x_min) / resolution).astype(np.int64), 0, cols - 1)
        row = np.clip(((y_max - points[:, 1]) / resolution).astype(np.int64), 0, rows - 1)
        return np.bincount(row * cols + col, minlength=rows * cols).astype(np.int32).reshape(rows, cols)

    span_high = min(obstacle_band[1], floor_span)
    occupied_hits = accumulate(xyz[(z >= ground + obstacle_band[0]) & (z <= ground + span_high)][:, :2])
    ground_hits = accumulate(xyz[(z >= ground + ground_band[0]) & (z <= ground + ground_band[1])][:, :2])
    image = np.full((rows, cols), PGM_UNKNOWN, dtype=np.uint8)
    image[ground_hits >= min_ground_hits] = PGM_FREE
    image[occupied_hits >= min_hits] = PGM_OCCUPIED
    in_floor = xyz[(z >= ground + ground_band[0]) & (z <= ground + floor_span)]
    return {"image": image, "origin": (x_min, y_min), "extent": bounds, "resolution": resolution,
            "floor_z": ground, "ceiling_z": ceiling, "obstacle_band": (obstacle_band[0], span_high),
            "ground_band": ground_band, "min_hits": min_hits, "min_ground_hits": min_ground_hits,
            "floor_cloud": in_floor}


# --------------------------------------------------------------------------
# writing -- pcd_to_floorgrid.py write_pgm / write_png / write_web_cloud
# --------------------------------------------------------------------------

def pgm_bytes(image: "np.ndarray") -> bytes:
    rows, cols = image.shape
    return "P5\n{} {}\n255\n".format(cols, rows).encode("ascii") + image.tobytes()


def png_bytes(image: "np.ndarray") -> bytes:
    """Greyscale PNG with the PGM's pixel values, via zlib (no Pillow)."""
    rows, cols = image.shape
    raw = b"".join(b"\x00" + image[r].tobytes() for r in range(rows))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", cols, rows, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def web_cloud_bytes(xyz: "np.ndarray", limit: int = 200_000, seed: int = 0) -> Tuple[bytes, int]:
    """A uniform sample of at most `limit` points as little-endian float32
    x,y,z triples (the console's 3D view). Seeded, so the same survey packs the
    same bytes."""
    rng = np.random.default_rng(seed)
    take = min(limit, len(xyz))
    chosen = xyz if take == len(xyz) else xyz[rng.choice(len(xyz), size=take, replace=False)]
    return np.ascontiguousarray(chosen, dtype="<f4").tobytes(), take


# --------------------------------------------------------------------------
# connectivity -- planning_grid.py components
# --------------------------------------------------------------------------

def components(mask: "np.ndarray", connectivity: int = 8) -> Tuple["np.ndarray", int]:
    """Connected labels (0 = not in mask) and their count. Copied from
    planning_grid.py components (breadth-first, one vectorised frontier step
    at a time)."""
    height, width = mask.shape
    w = width + 2
    padded = np.zeros((height + 2) * w, dtype=bool)
    padded.reshape(height + 2, w)[1:-1, 1:-1] = mask
    labels = np.zeros(padded.size, dtype=np.int32)
    if connectivity == 4:
        steps = np.array([-w, -1, 1, w], dtype=np.int64)
    elif connectivity == 8:
        steps = np.array([-w - 1, -w, -w + 1, -1, 1, w - 1, w, w + 1], dtype=np.int64)
    else:
        raise ValueError("connectivity is 4 or 8")
    count = 0
    for seed in np.flatnonzero(padded):
        if labels[seed]:
            continue
        count += 1
        labels[seed] = count
        frontier = np.array([seed], dtype=np.int64)
        while frontier.size:
            near = np.unique((frontier[:, None] + steps[None, :]).ravel())
            near = near[padded[near] & (labels[near] == 0)]
            labels[near] = count
            frontier = near
    return labels.reshape(height + 2, w)[1:-1, 1:-1], count
