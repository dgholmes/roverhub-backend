"""The map editor's cloud edits, and the "probably moved" layer it reviews.

Missions > Maps > Edit map (operator's pick, 2026-11-16) cleans a stored floor
in three ways. The brush paints the planning grid directly (PUT .../planning).
The other two edit the floor's CLOUD, and are this module:

  - delete boxes, drawn in the 3D map scene: every cloud point inside an
    axis-aligned box (map frame) goes;
  - "Remove" on the review list of the moved layer (moved.json, written by
    tools/map_prep/transient_filter.py): the blob's points go -- its bbox x
    its z_range, padded by BLOB_PAD_M because the file's numbers are rounded
    to the millimetre -- and the blob leaves the list. "Keep" only takes the
    blob off the list (PUT .../moved, or `keep_moved` here).

A cloud edit runs as a background job (maps/jobs.py, kind "cloud_edit"): a
floor's cloud is up to 8 MiB (~700k points) and its grid tens of millions of
cells, so it never runs on the event loop. It reads the floor under the
store's lock (MapStore.snapshot), works without it, and saves everything as
ONE new version with MapStore.edit_floor -- refused if another edit landed in
between, so nothing is lost to a stale read.

THE PLANNING GRID AFTER A CLOUD EDIT. Only the cells under the edited
footprints are looked at, and a cloud edit only ever FREES cells (removing
points cannot add an obstacle). A cell there is set free (254) only when all
of these hold:

  1. it was occupied before (an unknown cell is somewhere nobody looked, and
     stays unknown; a free cell stays free);
  2. no point is left in it in the evidence band -- planning_grid.py's low
     band (what a dog trips over, default 0.05-0.9 m above the floor) up to
     the top of the structure slice the planning grid also blocks on (the
     localisation map's band, 1.2-2.0 m by planning_grid.py's convention) --
     heights measured above the cloud's fitted floor plane, as planning_grid.py
     measures them (the stored floor_z_m when no plane fits);
  3. the edit took evidence out of it: a point of the cloud (or of moved.pcd,
     the moved layer's own points from the full survey) in that band was
     removed from that cell, or a delete box covers the whole cell and the
     whole band. The stored cloud is a SAMPLE of the survey (200k points of
     2.8 M on the office survey), so a cell with no sample point in it is not
     an empty cell -- a wall 5 cm thick has a coin-flip chance of none -- and
     re-rasterising "no points" as "free" would punch holes in walls. A cell
     with no evidence either way keeps what it was, and is counted
     (`kept_no_evidence_cells`) so the console can say "brush the rest";
  4. it is not inside a no_go or stairs zone, and is inside the union of the
     geofences when the map has any (planning_grid.py ZONE_RULES);
  5. it joins the walkable area the grid already had: 4-connected through
     such cells to a cell that was already free. Nothing becomes walkable
     that was not reachable before.

Because of 1 and 5 nothing outside the surveyed, walkable area the grid
already had can become walkable. Because the stored cloud is a sample, a
cell's remaining evidence counts with one point (`min_hits_effective` 1),
not planning_grid.py's --min-hits 3: one sampled return stands for many.

The band comes from the floor's survey.json `planning.parameters`
(planning_grid.py's stats `parameters` block: low_band_m) when it is there;
nothing writes that yet, so the defaults below are planning_grid.py's own
(--low-band 0.05,0.9) and the result says so (`parameters.source`).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .store import BundleError

MOVED_LAYER_VERSION = 1
MAX_MOVED_BLOBS = 10_000
MAX_BLOB_ID_CHARS = 64
MAX_POLYGON_VERTICES = 1_000
# A request's edits. A box is a few dozen bytes; 500 is far beyond a session
# of clicking and well under what makes the job slow.
MAX_DELETE_BOXES = 500
MAX_REASON_CHARS = 500
MAX_CLOUD_EDIT_BODY_BYTES = 1024 * 1024
# moved.json rounds to the millimetre; a blob's box is padded by this so a
# point on its edge is not left behind.
BLOB_PAD_M = 0.01

# tools/map_prep/planning_grid.py's defaults: --low-band 0.05,0.9 --min-hits 3,
# and the structure slice its --like map is made with (pcd_to_floorgrid.py
# --obstacle-band 1.2,2.0, planning_grid.py's header).
DEFAULT_LOW_BAND_M = (0.05, 0.9)
DEFAULT_MIN_HITS = 3
DEFAULT_STRUCTURE_BAND_M = (1.2, 2.0)
PARAMETERS_DEFAULT_SOURCE = "defaults (tools/map_prep/planning_grid.py --low-band 0.05,0.9 --min-hits 3)"

# planning_grid.py ZONE_RULES. A kind this table does not know excludes.
ZONE_RULES = {"no_go": "exclude", "geofence": "bound", "stairs": "exclude"}

PGM_FREE = 254
# Nav2's trinary read (store.OCCUPIED_THRESH / FREE_THRESH) as pixel values.
_OCCUPIED_BELOW = 255.0 * (1 - 0.65)
_FREE_ABOVE = 255.0 * (1 - 0.196)


# --------------------------------------------------------------------------
# strict JSON
# --------------------------------------------------------------------------

def _no_constants(name: str):
    raise ValueError("%s is not a number JSON allows" % name)


def strict_json(data: bytes, what: str):
    """json.loads, refusing NaN / Infinity (json.dumps would write them back,
    and a browser cannot parse that). Raises BundleError."""
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_no_constants)
    except UnicodeDecodeError:
        raise BundleError("%s is not UTF-8 text" % what) from None
    except ValueError as exc:
        raise BundleError("%s is not valid JSON: %s" % (what, exc)) from None


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --------------------------------------------------------------------------
# moved.json
# --------------------------------------------------------------------------

def _numbers(blob: dict, key: str, length: int, where: str) -> List[float]:
    value = blob.get(key)
    if not isinstance(value, list) or len(value) != length or not all(_finite(v) for v in value):
        raise BundleError("%s: %s must be %d finite numbers" % (where, key, length))
    return [float(v) for v in value]


def check_blob(blob, index: int) -> dict:
    """One blob of the layer, checked. Raises BundleError naming it."""
    where = "moved blob #%d" % index
    if not isinstance(blob, dict):
        raise BundleError(where + " is not an object")
    blob_id = blob.get("id")
    if not isinstance(blob_id, str) or not 0 < len(blob_id) <= MAX_BLOB_ID_CHARS:
        raise BundleError("%s: id must be a string of 1 to %d characters" % (where, MAX_BLOB_ID_CHARS))
    where = "moved blob %r" % blob_id
    _numbers(blob, "centroid", 2, where)
    xmin, ymin, xmax, ymax = _numbers(blob, "bbox", 4, where)
    if xmin > xmax or ymin > ymax:
        raise BundleError("%s: bbox must be [xmin, ymin, xmax, ymax] with min <= max" % where)
    zmin, zmax = _numbers(blob, "z_range", 2, where)
    if zmin > zmax:
        raise BundleError("%s: z_range must be [zmin, zmax] with zmin <= zmax" % where)
    if not _finite(blob.get("area_m2")) or blob["area_m2"] < 0:
        raise BundleError("%s: area_m2 must be a finite number of 0 or more" % where)
    for key in ("points", "hits", "pass_through"):
        if key in blob and not _count(blob[key]):
            raise BundleError("%s: %s must be a whole number of 0 or more" % (where, key))
    polygon = blob.get("polygon", [])
    if (not isinstance(polygon, list) or len(polygon) > MAX_POLYGON_VERTICES
            or not all(isinstance(p, list) and len(p) == 2 and all(_finite(v) for v in p) for p in polygon)):
        raise BundleError("%s: polygon must be at most %d finite [x, y] points" % (where, MAX_POLYGON_VERTICES))
    return blob


def parse_moved(data: bytes) -> dict:
    """moved.json, checked: {"version": 1, "source", "params", "blobs": [...]}.
    Raises BundleError (safe to show the uploader). The caller bounds the size."""
    doc = strict_json(data, "the moved layer")
    if not isinstance(doc, dict):
        raise BundleError("the moved layer must be a JSON object")
    if doc.get("version") != MOVED_LAYER_VERSION:
        raise BundleError("the moved layer's version is %r; this server reads version %d"
                          % (doc.get("version"), MOVED_LAYER_VERSION))
    blobs = doc.get("blobs")
    if not isinstance(blobs, list):
        raise BundleError("the moved layer has no blobs list")
    if len(blobs) > MAX_MOVED_BLOBS:
        raise BundleError("the moved layer has %d blobs; at most %d are kept" % (len(blobs), MAX_MOVED_BLOBS))
    seen = set()
    for index, blob in enumerate(blobs):
        check_blob(blob, index)
        if blob["id"] in seen:
            raise BundleError("moved blob id %r is used twice" % blob["id"])
        seen.add(blob["id"])
    if "source" in doc and doc["source"] is not None and not isinstance(doc["source"], str):
        raise BundleError("the moved layer's source must be a string")
    if "params" in doc and not isinstance(doc["params"], dict):
        raise BundleError("the moved layer's params must be an object")
    return doc


def moved_bytes(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def blob_box(blob: dict, pad: float = BLOB_PAD_M) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """(min, max) of a blob's points: its bbox x its z_range, padded."""
    xmin, ymin, xmax, ymax = blob["bbox"]
    zmin, zmax = blob["z_range"]
    return ((xmin - pad, ymin - pad, zmin - pad), (xmax + pad, ymax + pad, zmax + pad))


def rotate_moved(doc: dict, rotation, angle_deg: float) -> dict:
    """The moved layer carried through the survey's levelling rotation R
    (apply as xyz @ R.T, maps/survey.py). A blob's box becomes the box around
    its eight rotated corners -- a little larger, never smaller -- its
    centroid and polygon are rotated at the middle of its z range."""
    R = np.asarray(rotation, dtype=np.float64)
    out = []
    for blob in doc["blobs"]:
        xmin, ymin, xmax, ymax = blob["bbox"]
        zmin, zmax = blob["z_range"]
        corners = np.array([[x, y, z] for x in (xmin, xmax) for y in (ymin, ymax) for z in (zmin, zmax)]) @ R.T
        mid = (zmin + zmax) / 2.0
        centroid = np.array([[blob["centroid"][0], blob["centroid"][1], mid]]) @ R.T
        polygon = blob.get("polygon") or []
        turned = (np.array([[x, y, mid] for x, y in polygon]) @ R.T) if polygon else np.zeros((0, 3))
        out.append({**blob,
                    "centroid": [round(float(v), 3) for v in centroid[0, :2]],
                    "bbox": [round(float(corners[:, 0].min()), 3), round(float(corners[:, 1].min()), 3),
                             round(float(corners[:, 0].max()), 3), round(float(corners[:, 1].max()), 3)],
                    "z_range": [round(float(corners[:, 2].min()), 3), round(float(corners[:, 2].max()), 3)],
                    "polygon": [[round(float(x), 3), round(float(y), 3)] for x, y, _ in turned]})
    params = dict(doc.get("params") or {})
    params["levelled_by_server_deg"] = round(float(angle_deg), 4)
    return {**doc, "params": params, "blobs": out}


# --------------------------------------------------------------------------
# PCD (moved.pcd): read and write, zero points allowed
# --------------------------------------------------------------------------

def read_pcd_bytes(data: bytes, max_points: int) -> "np.ndarray":
    """(N, 3) float64 x, y, z of an ascii or binary PCD held in memory. N may be
    0 (nothing moved). Raises BundleError. survey_math.read_pcd_xyz is the
    survey's reader; this one takes bytes and an empty cloud."""
    fields: Dict[str, List[str]] = {}
    offset = 0
    for _ in range(64):
        end = data.find(b"\n", offset)
        if end < 0:
            raise BundleError("the moved cloud ends before its DATA line; is it a .pcd?")
        line = data[offset:end].decode("ascii", "replace").strip()
        offset = end + 1
        if not line or line.startswith("#"):
            continue
        key, _, rest = line.partition(" ")
        fields[key.upper()] = rest.split()
        if key.upper() == "DATA":
            break
    else:
        raise BundleError("the moved cloud has no DATA line in its first 64 lines; is it a .pcd?")
    try:
        names = fields["FIELDS"]
        sizes = [int(v) for v in fields["SIZE"]]
        types = [t.upper() for t in fields["TYPE"]]
        counts = [int(v) for v in fields.get("COUNT", ["1"] * len(names))]
        points = int(fields["POINTS"][0]) if "POINTS" in fields else int(fields["WIDTH"][0])
        kind = fields["DATA"][0].lower()
    except (KeyError, IndexError, ValueError):
        raise BundleError("the moved cloud's PCD header is missing FIELDS, SIZE, TYPE or POINTS") from None
    if not {"x", "y", "z"} <= set(names):
        raise BundleError("the moved cloud has no x, y, z fields")
    if not 0 <= points <= max_points:
        raise BundleError("the moved cloud says it holds %d points; at most %d are kept" % (points, max_points))
    if points == 0:
        return np.zeros((0, 3))
    if kind == "binary":
        codes = {"F": "f", "U": "u", "I": "i"}
        if not len(names) == len(sizes) == len(types) == len(counts):
            raise BundleError("the moved cloud's FIELDS, SIZE, TYPE and COUNT lines differ in length")
        parts = []
        for name, size, type_, count in zip(names, sizes, types, counts, strict=True):
            if count != 1 or type_ not in codes or size not in (1, 2, 4, 8):
                raise BundleError("the moved cloud's field %r is not a single scalar" % name)
            parts.append((name, "<" + codes[type_] + str(size)))
        dtype = np.dtype(parts)
        if len(data) - offset < dtype.itemsize * points:
            raise BundleError("the moved cloud is shorter than its header says")
        array = np.frombuffer(data, dtype=dtype, count=points, offset=offset)
        xyz = np.stack([array["x"], array["y"], array["z"]], axis=1).astype(np.float64)
    elif kind == "ascii":
        column = {name: i for i, name in enumerate(names)}
        rows = []
        for line in data[offset:].decode("ascii", "replace").splitlines():
            parts_ = line.split()
            if not parts_:
                continue
            try:
                rows.append([float(parts_[column["x"]]), float(parts_[column["y"]]), float(parts_[column["z"]])])
            except (ValueError, IndexError):
                raise BundleError("the moved cloud's ascii data does not parse") from None
            if len(rows) == points:
                break
        if len(rows) < points:
            raise BundleError("the moved cloud is shorter than its header says")
        xyz = np.asarray(rows, dtype=np.float64).reshape(-1, 3)
    else:
        raise BundleError("the moved cloud's DATA is %r; ascii or binary is handled" % kind)
    return xyz[np.isfinite(xyz).all(axis=1)]


def pcd_bytes(xyz: "np.ndarray") -> bytes:
    """Binary float32 x y z PCD, as transient_filter.py write_pcd writes it."""
    out = np.ascontiguousarray(xyz, dtype="<f4")
    header = ("# .PCD v0.7 - written by roverhub-backend maps/cloud_edit.py\n"
              "VERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
              "WIDTH {n}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS {n}\nDATA binary\n").format(n=len(out))
    return header.encode("ascii") + out.tobytes()


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------

Box = Tuple[Tuple[float, float, float], Tuple[float, float, float]]


@dataclass
class CloudEdit:
    delete_boxes: List[Box] = field(default_factory=list)
    resolve_moved: List[str] = field(default_factory=list)
    keep_moved: List[str] = field(default_factory=list)
    reason: Optional[str] = None


def _ids(doc: dict, key: str) -> List[str]:
    value = doc.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and 0 < len(v) <= MAX_BLOB_ID_CHARS
                                             for v in value):
        raise BundleError("%s must be a list of blob ids" % key)
    if len(value) > MAX_MOVED_BLOBS:
        raise BundleError("%s names more than %d blobs" % (key, MAX_MOVED_BLOBS))
    if len(set(value)) != len(value):
        raise BundleError("%s names a blob twice" % key)
    return list(value)


def parse_cloud_edit(data: bytes) -> CloudEdit:
    """The POST .../cloud-edits body, checked. Raises BundleError (a 400)."""
    doc = strict_json(data, "the body")
    if not isinstance(doc, dict):
        raise BundleError('the body must be {"delete_boxes": [...], "resolve_moved": [...], "reason": "..."}')
    unknown = sorted(set(doc) - {"delete_boxes", "resolve_moved", "keep_moved", "reason"})
    if unknown:
        raise BundleError("unknown field(s): %s" % ", ".join(unknown))
    raw_boxes = doc.get("delete_boxes", [])
    if not isinstance(raw_boxes, list):
        raise BundleError("delete_boxes must be a list")
    if len(raw_boxes) > MAX_DELETE_BOXES:
        raise BundleError("delete_boxes has %d boxes; at most %d are taken at once"
                          % (len(raw_boxes), MAX_DELETE_BOXES))
    boxes: List[Box] = []
    for index, box in enumerate(raw_boxes):
        where = "delete box #%d" % index
        if not isinstance(box, dict) or set(box) != {"min", "max"}:
            raise BundleError('%s must be {"min": [x, y, z], "max": [x, y, z]}' % where)
        corners = []
        for key in ("min", "max"):
            value = box[key]
            if not isinstance(value, list) or len(value) != 3 or not all(_finite(v) for v in value):
                raise BundleError("%s: %s must be three finite numbers [x, y, z]" % (where, key))
            corners.append(tuple(float(v) for v in value))
        low, high = corners
        if any(a > b for a, b in zip(low, high, strict=True)):
            raise BundleError("%s: min is above max on an axis (min %s, max %s)" % (where, list(low), list(high)))
        boxes.append((low, high))  # type: ignore[arg-type]
    resolve = _ids(doc, "resolve_moved")
    keep = _ids(doc, "keep_moved")
    both = sorted(set(resolve) & set(keep))
    if both:
        raise BundleError("blob(s) %s are both removed and kept" % ", ".join(both))
    reason = doc.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS):
        raise BundleError("reason must be text of at most %d characters" % MAX_REASON_CHARS)
    if not boxes and not resolve and not keep:
        raise BundleError("nothing to do: give delete_boxes, resolve_moved or keep_moved")
    return CloudEdit(delete_boxes=boxes, resolve_moved=resolve, keep_moved=keep, reason=reason)


# --------------------------------------------------------------------------
# geometry: planning_grid.py's frame, bands and zone rules
# --------------------------------------------------------------------------

@dataclass
class Frame:
    """planning_grid.py Frame: column 0 is min x, row 0 is MAX y."""
    resolution: float
    origin: Tuple[float, float]
    width: int
    height: int

    @property
    def shape(self) -> Tuple[int, int]:
        return self.height, self.width

    def cells(self, xy: "np.ndarray") -> Tuple["np.ndarray", "np.ndarray", "np.ndarray"]:
        """(row, col, inside) for (N, 2) map-frame points; outside points are
        flagged, never clipped onto the edge (planning_grid.py Frame.cells)."""
        col = np.floor((xy[:, 0] - self.origin[0]) / self.resolution).astype(np.int64)
        j = np.floor((xy[:, 1] - self.origin[1]) / self.resolution).astype(np.int64)
        inside = (col >= 0) & (col < self.width) & (j >= 0) & (j < self.height)
        return self.height - 1 - j, col, inside

    def centres(self, rows: "np.ndarray", cols: "np.ndarray") -> Tuple["np.ndarray", "np.ndarray"]:
        xs = self.origin[0] + (np.asarray(cols) + 0.5) * self.resolution
        ys = self.origin[1] + (self.height - 1 - np.asarray(rows) + 0.5) * self.resolution
        return xs, ys

    def rect_cells(self, xmin: float, ymin: float, xmax: float, ymax: float,
                   whole_only: bool = False) -> Optional[Tuple[int, int, int, int]]:
        """(row0, row1, col0, col1), inclusive, of the cells a rectangle
        overlaps -- or, `whole_only`, lies wholly over. None when none."""
        res, (ox, oy) = self.resolution, self.origin
        if whole_only:
            c0 = math.ceil((xmin - ox) / res - 1e-9)
            c1 = math.floor((xmax - ox) / res + 1e-9) - 1
            j0 = math.ceil((ymin - oy) / res - 1e-9)
            j1 = math.floor((ymax - oy) / res + 1e-9) - 1
        else:
            c0 = math.floor((xmin - ox) / res)
            c1 = max(c0, math.ceil((xmax - ox) / res) - 1)
            j0 = math.floor((ymin - oy) / res)
            j1 = max(j0, math.ceil((ymax - oy) / res) - 1)
        c0, c1 = max(c0, 0), min(c1, self.width - 1)
        j0, j1 = max(j0, 0), min(j1, self.height - 1)
        if c0 > c1 or j0 > j1:
            return None
        return self.height - 1 - j1, self.height - 1 - j0, c0, c1


def floor_heights(points: "np.ndarray", floor_z: float, plane: Optional[Dict]) -> "np.ndarray":
    """The height of each point above the floor under it (planning_grid.py floor_height)."""
    if plane is None:
        return points[:, 2] - floor_z
    return points[:, 2] - (plane["a"] * points[:, 0] + plane["b"] * points[:, 1] + plane["c"])


def band_hits(frame: Frame, cloud: "np.ndarray", floor_z: float, plane: Optional[Dict],
              band: Tuple[float, float]) -> "np.ndarray":
    """Points per cell whose height above the floor is within `band`
    (inclusive), (height, width) -- planning_grid.py build()'s step 1 rule,
    `hits >= --min-hits` being its low obstacles."""
    counts = np.zeros(frame.width * frame.height, dtype=np.int64)
    if len(cloud) == 0:
        return counts.reshape(frame.shape)
    height = floor_heights(cloud, floor_z, plane)
    chosen = cloud[(height >= band[0]) & (height <= band[1])]
    rows, cols, inside = frame.cells(chosen[:, :2])
    np.add.at(counts, rows[inside] * frame.width + cols[inside], 1)
    return counts.reshape(frame.shape)


def inside_polygon(xs: "np.ndarray", ys: "np.ndarray", polygon: Sequence[Sequence[float]]) -> "np.ndarray":
    """Points (cell centres) inside the polygon, even-odd (planning_grid.py inside_polygon)."""
    poly = np.asarray(polygon, dtype=np.float64)
    inside = np.zeros(np.shape(xs), dtype=bool)
    for (x1, y1), (x2, y2) in zip(poly, np.roll(poly, -1, axis=0), strict=True):
        crosses = (y1 > ys) != (y2 > ys)
        if not crosses.any():
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = x1 + (ys - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (xs < x_at)
    return inside


def flood(mask: "np.ndarray", seeds: "np.ndarray") -> "np.ndarray":
    """The cells of `mask` 4-connected to a seed in it (planning_grid.py flood)."""
    height, width = mask.shape
    w = width + 2
    padded = np.zeros((height + 2) * w, dtype=bool)
    padded.reshape(height + 2, w)[1:-1, 1:-1] = mask
    reached = np.zeros_like(padded)
    reached.reshape(height + 2, w)[1:-1, 1:-1] = seeds & mask
    steps = np.array([-w, -1, 1, w], dtype=np.int64)
    frontier = np.flatnonzero(reached)
    while frontier.size:
        near = np.unique((frontier[:, None] + steps[None, :]).ravel())
        near = near[padded[near] & ~reached[near]]
        reached[near] = True
        frontier = near
    return reached.reshape(height + 2, w)[1:-1, 1:-1]


def in_boxes(xyz: "np.ndarray", boxes: Sequence[Box]) -> Tuple["np.ndarray", List[int]]:
    """(mask of points inside any box, points inside each box). Inclusive bounds.
    Sorted by x once, so each box looks only at its own x slab."""
    mask = np.zeros(len(xyz), dtype=bool)
    per_box: List[int] = []
    if len(xyz) == 0 or not boxes:
        return mask, [0] * len(boxes)
    order = np.argsort(xyz[:, 0], kind="stable")
    xs = xyz[order, 0]
    for (x0, y0, z0), (x1, y1, z1) in boxes:
        lo, hi = np.searchsorted(xs, x0, side="left"), np.searchsorted(xs, x1, side="right")
        idx = order[lo:hi]
        sub = xyz[idx]
        hit = (sub[:, 1] >= y0) & (sub[:, 1] <= y1) & (sub[:, 2] >= z0) & (sub[:, 2] <= z1)
        per_box.append(int(hit.sum()))
        mask[idx[hit]] = True
    return mask, per_box


def planning_parameters(survey_doc: Optional[dict]) -> Tuple[Tuple[float, float], Tuple[float, float], str]:
    """(low band, structure band, where they came from)."""
    params = None
    if isinstance(survey_doc, dict) and isinstance(survey_doc.get("planning"), dict):
        params = survey_doc["planning"].get("parameters")
    if isinstance(params, dict):
        low = params.get("low_band_m")
        structure = params.get("structure_band_m", list(DEFAULT_STRUCTURE_BAND_M))
        if (isinstance(low, list) and len(low) == 2 and all(_finite(v) for v in low) and low[0] < low[1]
                and isinstance(structure, list) and len(structure) == 2 and all(_finite(v) for v in structure)
                and structure[0] < structure[1]):
            return (float(low[0]), float(low[1])), (float(structure[0]), float(structure[1])), \
                "survey.json planning.parameters"
    return DEFAULT_LOW_BAND_M, DEFAULT_STRUCTURE_BAND_M, PARAMETERS_DEFAULT_SOURCE


def rerasterise(pixels: "np.ndarray", frame: Frame, remaining: "np.ndarray", removed: "np.ndarray",
                floor_z: float, plane: Optional[Dict], band: Tuple[float, float],
                footprints: Sequence[Tuple[float, float, float, float]], covering_boxes: Sequence[Box],
                zones: Sequence) -> Tuple["np.ndarray", Dict]:
    """The planning grid after a cloud edit; see the module docstring for the
    rule. `pixels` (height, width) uint8 is not changed. `remaining` is the
    cloud after the edit; `removed` every point the edit took out (cloud and
    moved.pcd); `footprints` the (xmin, ymin, xmax, ymax) of every edited box;
    `covering_boxes` the delete boxes, which free a cell they wholly cover."""
    out = pixels.copy()
    stats = {"footprint_cells": 0, "occupied_cells_in_footprint": 0, "freed_cells": 0,
             "kept_returns_cells": 0, "kept_zone_cells": 0, "kept_no_evidence_cells": 0,
             "kept_unconnected_cells": 0}
    footprint = np.zeros(frame.shape, dtype=bool)
    for xmin, ymin, xmax, ymax in footprints:
        cells = frame.rect_cells(xmin, ymin, xmax, ymax)
        if cells is not None:
            r0, r1, c0, c1 = cells
            footprint[r0:r1 + 1, c0:c1 + 1] = True
    if not footprint.any():
        return out, stats
    rows_any, cols_any = np.flatnonzero(footprint.any(axis=1)), np.flatnonzero(footprint.any(axis=0))
    r0, r1 = max(0, rows_any[0] - 1), min(frame.height - 1, rows_any[-1] + 1)
    c0, c1 = max(0, cols_any[0] - 1), min(frame.width - 1, cols_any[-1] + 1)
    window = (slice(r0, r1 + 1), slice(c0, c1 + 1))

    def only_window(points: "np.ndarray") -> "np.ndarray":
        if len(points) == 0:
            return points
        x_lo = frame.origin[0] + c0 * frame.resolution
        x_hi = frame.origin[0] + (c1 + 1) * frame.resolution
        y_hi = frame.origin[1] + (frame.height - r0) * frame.resolution
        y_lo = frame.origin[1] + (frame.height - 1 - r1) * frame.resolution
        keep = (points[:, 0] >= x_lo) & (points[:, 0] < x_hi) & (points[:, 1] >= y_lo) & (points[:, 1] < y_hi)
        return points[keep]

    left = band_hits(frame, only_window(remaining), floor_z, plane, band)[window]
    lost = band_hits(frame, only_window(removed), floor_z, plane, band)[window]
    foot = footprint[window]
    before = pixels[window]
    occupied = before < _OCCUPIED_BELOW
    free_before = before > _FREE_ABOVE

    rr, cc = np.meshgrid(np.arange(r0, r1 + 1), np.arange(c0, c1 + 1), indexing="ij")
    xs, ys = frame.centres(rr, cc)
    covered = np.zeros(foot.shape, dtype=bool)
    for (bx0, by0, bz0), (bx1, by1, bz1) in covering_boxes:
        cells = frame.rect_cells(bx0, by0, bx1, by1, whole_only=True)
        if cells is None:
            continue
        rows = slice(max(cells[0], r0) - r0, min(cells[1], r1) - r0 + 1)
        cols = slice(max(cells[2], c0) - c0, min(cells[3], c1) - c0 + 1)
        ground = floor_z if plane is None else plane["a"] * xs[rows, cols] + plane["b"] * ys[rows, cols] + plane["c"]
        spans = (bz0 <= ground + band[0]) & (bz1 >= ground + band[1])
        covered[rows, cols] |= spans

    allowed = np.ones(foot.shape, dtype=bool)
    within = None
    for zone in zones:
        kind = getattr(zone, "kind", None)
        polygon = [tuple(p) for p in getattr(zone, "polygon", [])]
        if len(polygon) < 3:
            continue
        inside = inside_polygon(xs, ys, polygon)
        if ZONE_RULES.get(kind, "exclude") == "bound":
            within = inside if within is None else (within | inside)
        else:
            allowed &= ~inside
    if within is not None:
        allowed &= within

    target = foot & occupied
    has_returns = target & (left > 0)
    zone_blocked = target & ~has_returns & ~allowed
    evidence = (lost > 0) | covered
    no_evidence = target & ~has_returns & allowed & ~evidence
    candidate = target & ~has_returns & allowed & evidence
    reached = flood(candidate | free_before, free_before)
    freed = candidate & reached
    out[window][freed] = PGM_FREE
    stats.update({
        "footprint_cells": int(foot.sum()),
        "occupied_cells_in_footprint": int(target.sum()),
        "freed_cells": int(freed.sum()),
        "kept_returns_cells": int(has_returns.sum()),
        "kept_zone_cells": int(zone_blocked.sum()),
        "kept_no_evidence_cells": int(no_evidence.sum()),
        "kept_unconnected_cells": int((candidate & ~reached).sum()),
    })
    return out, stats


# --------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------

def check_against_floor(edit: CloudEdit, has_cloud: bool, moved: Optional[dict]) -> None:
    """Refuse an edit the floor cannot take: deleting from a floor with no
    cloud, or naming a blob its moved layer does not have."""
    if (edit.delete_boxes or edit.resolve_moved) and not has_cloud:
        raise BundleError("this floor has no cloud to edit")
    named = edit.resolve_moved + edit.keep_moved
    if named:
        if moved is None:
            raise BundleError("this floor has no moved layer, so there is no blob %s" % named[0])
        known = {blob["id"] for blob in moved["blobs"]}
        missing = [blob_id for blob_id in named if blob_id not in known]
        if missing:
            raise BundleError("the moved layer has no blob(s) %s" % ", ".join(missing[:10]))


def run_cloud_edit(store, map_id: str, floor_index: int, edit: CloudEdit,
                   progress=lambda stage: None, detail: Optional[dict] = None) -> dict:
    """Apply `edit` to one floor and store the result as one new version.
    Returns the job result. Raises BundleError (shown to the editor) when the
    edit cannot be made, or the map changed while it was being made."""
    from . import survey_math as sm
    from .store import MAX_CLOUD_BYTES, MapNotFound, parse_pgm, planning_grid_for

    progress("reading the floor")
    try:
        site_map, version, files = store.snapshot(map_id, ("cloud", "planning", "moved", "moved_cloud", "survey"))
    except MapNotFound:
        raise BundleError("map %s was deleted before the edit could be made" % map_id) from None
    floor = next((f for f in site_map.floors if f.index == floor_index), None)
    if floor is None:
        raise BundleError("map %s has no floor %d any more" % (map_id, floor_index))
    have = files[floor_index]
    moved = parse_moved(have["moved"]) if have["moved"] is not None else None
    check_against_floor(edit, have["cloud"] is not None, moved)

    cloud = np.zeros((0, 3))
    if have["cloud"] is not None:
        if len(have["cloud"]) % 12:
            raise BundleError("the floor's cloud is not whole float32 x, y, z triples")
        cloud = np.frombuffer(have["cloud"], dtype="<f4").reshape(-1, 3).astype(np.float64)
    moved_cloud = None
    if have["moved_cloud"] is not None:
        moved_cloud = read_pcd_bytes(have["moved_cloud"], MAX_CLOUD_BYTES // 12)

    blobs = {blob["id"]: blob for blob in (moved["blobs"] if moved else [])}
    blob_boxes = [blob_box(blobs[blob_id]) for blob_id in edit.resolve_moved]
    kept_boxes = [blob_box(blobs[blob_id]) for blob_id in edit.keep_moved]
    cut = list(edit.delete_boxes) + blob_boxes

    progress("removing points")
    remove, per_box = in_boxes(cloud, cut)
    remaining = cloud[~remove]
    removed = cloud[remove]
    moved_removed = 0
    new_moved_cloud = None
    if moved_cloud is not None and (cut or kept_boxes):
        # The amber layer shows what is still listed: points the edit took
        # out of the map, and the points of blobs taken off the list either way.
        gone, _ = in_boxes(moved_cloud, cut + kept_boxes)
        lost_moved, _ = in_boxes(moved_cloud, cut)
        removed = np.vstack([removed, moved_cloud[lost_moved]])
        moved_removed = int(gone.sum())
        if moved_removed:
            new_moved_cloud = pcd_bytes(moved_cloud[~gone])

    plane = sm.fit_floor_plane(cloud) if len(cloud) >= 3 else None
    survey_doc = None
    if have["survey"] is not None:
        try:
            survey_doc = json.loads(have["survey"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            survey_doc = None
    low_band, structure_band, source = planning_parameters(survey_doc)
    band = (low_band[0], max(low_band[1], structure_band[1]))
    planning_result: Dict = {
        "rerasterised": False, "reason": None,
        "parameters": {"low_band_m": list(low_band), "structure_band_m": list(structure_band),
                       "evidence_band_m": list(band), "min_hits": DEFAULT_MIN_HITS, "min_hits_effective": 1,
                       "heights_from": "floor plane" if plane is not None else "floor_z_m",
                       "source": source,
                       "note": "the stored cloud is a sample of the survey, so one remaining return in the "
                               "evidence band keeps a cell as it was, and a cell is freed only where the edit "
                               "took evidence out of it (or a delete box covers it and the whole band)"},
    }
    new_planning = None
    if have["planning"] is None:
        planning_result["reason"] = "this floor has no planning grid"
    elif not cut:
        planning_result["reason"] = "nothing was deleted (only blobs kept)"
    else:
        progress("re-rasterising the planning grid")
        width, height, raw = parse_pgm(have["planning"])
        if (width, height) != tuple(floor.size_cells):
            raise BundleError("the floor's planning grid is not the floor's size")
        pixels = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
        frame = Frame(floor.resolution_m, (floor.origin_xy_m[0], floor.origin_xy_m[1]), width, height)
        footprints = [(lo[0], lo[1], hi[0], hi[1]) for lo, hi in cut]
        image, stats = rerasterise(pixels, frame, remaining, removed, floor.floor_z_m, plane, band,
                                   footprints, edit.delete_boxes, site_map.zones)
        planning_result.update(stats)
        planning_result["rerasterised"] = True
        if stats["freed_cells"]:
            new_planning = b"P5\n%d %d\n255\n" % (width, height) + image.tobytes()

    new_moved = None
    if moved is not None and (edit.resolve_moved or edit.keep_moved):
        dropped = set(edit.resolve_moved) | set(edit.keep_moved)
        new_moved = moved_bytes({**moved, "blobs": [b for b in moved["blobs"] if b["id"] not in dropped]})

    progress("saving a new version")
    clearance = floor.planning.clearance_m if floor.planning is not None else None

    def change(current, current_floor, floor_dir, write) -> dict:
        if int(remove.sum()):
            write("cloud", np.ascontiguousarray(remaining, dtype="<f4").tobytes())
        if new_moved is not None:
            write("moved", new_moved)
        if new_moved_cloud is not None:
            write("moved_cloud", new_moved_cloud)
        if new_planning is not None:
            planning = planning_grid_for(current_floor, new_planning, clearance)
            write("planning", new_planning)
            return {"planning": planning}
        return {}

    record = {"floor": floor_index, "boxes": len(edit.delete_boxes), "resolved_moved": len(edit.resolve_moved),
              "kept_moved": len(edit.keep_moved), "points_removed": int(remove.sum()),
              "cells_freed": planning_result.get("freed_cells", 0), "reason": edit.reason, **(detail or {})}
    try:
        saved, number = store.edit_floor(map_id, floor_index, change, source="cloud_edit",
                                         detail=record, expect_version=version)
    except MapNotFound:  # FloorNotFound too
        raise BundleError("map %s or its floor %d was removed while the edit was being made"
                          % (map_id, floor_index)) from None
    saved_floor = next(f for f in saved.floors if f.index == floor_index)
    if saved_floor.planning is not None:
        planning_result.update({"free_cells": saved_floor.planning.free_cells,
                                "occupied_cells": saved_floor.planning.occupied_cells,
                                "unknown_cells": saved_floor.planning.unknown_cells})
    return {
        "map_id": map_id, "floor": floor_index, "version": number, "previous_version": version,
        "status": saved.status,
        "points_before": int(len(cloud)), "points_removed": int(remove.sum()),
        "points_after": int(len(remaining)), "points_removed_per_box": per_box[:len(edit.delete_boxes)],
        "resolved_moved": list(edit.resolve_moved), "kept_moved": list(edit.keep_moved),
        "moved_blobs_left": None if moved is None else
        len(moved["blobs"]) - len(edit.resolve_moved) - len(edit.keep_moved),
        "moved_cloud_points_removed": moved_removed,
        "planning": planning_result,
        "reason": edit.reason,
    }
