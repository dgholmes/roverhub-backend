"""File-backed site maps.

A survey is packed into a *bundle* directory by tools/map_prep/pack_map.py:

    site_map.json       {"site_map": SiteMap fields, "floors": [MapFloor fields
                         + artifact names], "provenance": ...}
    floor<N>/grid.pgm   the occupancy grid
    floor<N>/grid.png   the same pixels, for a browser
    floor<N>/cloud.bin  float32 x,y,z triples, if a cloud was packed
    floor<N>/planning.pgm  the planning grid Nav2 plans on, if one was made
                        (tools/map_prep/planning_grid.py); optional

`save_bundle` takes such a directory and keeps it under `<root>/<map_id>/` as

    map.json            the validated SiteMap (floors joined in)
    version.json        which version of this map's bundle this is (VERSIONS below)
    quality.json        the quality report, once one has run (maps/quality.py)
    floor<N>/...        the artifacts, byte for byte
    floor<N>/track.csv  the path the survey walked (t,x,y,z, level), optional
    floor<N>/survey.json  how the server levelled and gridded it (maps/survey.py)
    versions/v<K>/      earlier bundles, each laid out as above

VERSIONS (tracker 1-5-06). Replacing a map's bundle -- a re-upload, a survey
processed by maps/survey.py, or a restore -- keeps the bundle it replaces
under versions/v<K>/ (hard links where the filesystem allows, so nothing is
copied: every file here is replaced, never rewritten in place). The newest
MAX_VERSIONS are kept. `restore_version` makes an old one current again under
a new version number, so a restore can itself be undone. The zones carry over
in both directions (they are the map's, not a survey's) and the map drops to
draft, exactly as on a re-upload; the restored floors keep the planning grids
they had, since those were made in that survey's frame.

A floor's planning grid can also be set or removed on a stored map
(`set_planning`, `delete_planning`): it is usually made after the survey, from
the survey, on the machine that runs Nav2.

Every floor carries a content hash (`MapFloor.content_hash`, tracker 1-5-01):
sha256 over its grid's and planning grid's pixels and placement and the map's
zones, as shared/schemas/map_hash.py defines it. It is recomputed from the
stored files on every write that can change it -- a bundle, a planning grid,
the zones -- and never taken from an upload. A map stored before the hash
existed gets one computed on read (not written back) until its next write. The
bridge hashes its own copies the same way, so the two numbers say whether the
robot runs the map the console shows.

No database, like runs/store.py: the directory is re-read on every call.

Two things here take caller-supplied text and must not be talked into reading or
writing outside the store: the `map_id` (it names a directory) and, in
`artifact_path`, the floor and kind (they name files). Neither is ever joined
onto a path unchecked. The id is matched against an allowlist, and `kind` is a
lookup into a fixed table -- the caller's string is a key, never a path part.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, get_args

from pydantic import ValidationError

from shared.schemas.map_hash import MapHashError, content_hash, grid_entry, zone_entries
from shared.schemas.maps import MapFloor, MapStatus, MapZone, PlanningGrid, SiteMap

logger = logging.getLogger("roverhub.maps")

DEFAULT_MAPS_DIR = str(Path(__file__).resolve().parents[1] / "data" / "maps")

# Mirrors MAX_CLOUD_BYTES in tools/map_prep/pack_map.py, which refuses a larger
# cloud at packing time. The router enforces it again on upload and on serving,
# so the two figures must stay the same expression: 8_000_000 here would let a
# cloud pass packing and fail serving by 388,608 bytes.
MAX_CLOUD_BYTES = 8 * 1024 * 1024

# Bounds on what a curator may store in one map. Generous for a building (a
# zone is a room or a doorway), small enough that a request cannot fill the disk
# or make every map read slow.
MAX_ZONES = 256
MAX_VERTICES_PER_ZONE = 1000

BUNDLE_FILE = "site_map.json"
MAP_FILE = "map.json"

# kind (as it appears in a URL) -> file name inside floor<N>/. The whole of what
# a caller may ask for.
ARTIFACT_FILES = {"grid": "grid.pgm", "preview": "grid.png", "cloud": "cloud.bin",
                  "planning": "planning.pgm", "track": "track.csv", "survey": "survey.json"}
_REQUIRED = ("grid", "preview")
PLANNING_FILE = ARTIFACT_FILES["planning"]

# The planner's padded half-width (PlanningGrid.clearance_m's default).
DEFAULT_CLEARANCE_M = 0.2
# A planning grid is one byte per cell: 32 MiB is a 5,600 x 5,600 grid, 280 m a
# side at 5 cm, and the same as an image part of an upload (router.MAX_IMAGE_BYTES).
MAX_PLANNING_BYTES = 32 * 1024 * 1024

# How Nav2's map_server reads a trinary PGM (negate 0): a cell is occupied when
# (255 - v) / 255 > occupied_thresh, free when it is < free_thresh, and unknown
# otherwise. The figures are the planning grid's own yaml's (planning_grid.py),
# and the expression is planning_grid.py's read_like(), so the counts here are
# the counts Nav2 sees: 0 is occupied, 254 free, 205 unknown.
OCCUPIED_THRESH = 0.65
FREE_THRESH = 0.196
_CELL_CLASS = bytes(
    ord("o") if (255.0 - v) / 255.0 > OCCUPIED_THRESH
    else ord("f") if (255.0 - v) / 255.0 < FREE_THRESH
    else ord("u")
    for v in range(256))

# tools/map_prep/pack_map.py's header pattern. Nav2's map_saver puts a
# `# CREATOR:` comment between the magic number and the size; exactly one
# whitespace byte follows maxval, and the pixels start right after it.
_PGM_HEADER = re.compile(
    rb"(?:\s|#[^\n]*\n)*P5(?:\s|#[^\n]*\n)+(\d+)(?:\s|#[^\n]*\n)+(\d+)(?:\s|#[^\n]*\n)+(\d+)\s")

# Concatenated into a path, so allowlisted. Must start with a letter or digit:
# that rules out "." and "..", and makes every scratch name below (which starts
# with ".") impossible to collide with a real map. No trailing "." -- Windows
# drops it, so "map." and "map" would be one directory.
MAP_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9_-])?")
# A directory named like a Windows device ("con", "nul.x") is not a directory.
_WINDOWS_DEVICES = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", re.IGNORECASE)

# Working directories beside the maps. All start with "." so MAP_ID_RE cannot
# produce one. `.incoming-` is a bundle being assembled; `.upload-` is the
# router's copy of an upload; `.deleted-` is a map on its way out; `.replaced-`
# holds the OLD map for the instant a re-upload swaps the new one in.
_SCRATCH_PREFIX = ".incoming-"
UPLOAD_PREFIX = ".upload-"
_DELETED_PREFIX = ".deleted-"
_OLD_PREFIX = ".replaced-"

VERSIONS_DIR = "versions"
VERSION_FILE = "version.json"
QUALITY_FILE = "quality.json"
# Earlier bundles kept per map. A bundle is a few MB to a few tens of MB (the
# grids and a cloud of at most 8 MiB per floor), so ten is room for a site's
# re-surveys over months without a map directory growing past a gigabyte.
MAX_VERSIONS = 10
_VERSION_DIR_RE = re.compile(r"v([1-9][0-9]{0,8})")
# A track is a few dozen bytes per pose at ~10 Hz: 8 MiB is hours of walking.
MAX_TRACK_BYTES = 8 * 1024 * 1024


class BundleError(ValueError):
    """The bundle is not a usable map. The message is safe to show the uploader."""


def _check_floor(floor: MapFloor) -> None:
    """Refuse a floor whose numbers cannot all be true at once.

    MapFloor takes whatever it is given, by design: the bundle is where corrupt
    numbers get caught, at both ends. This is the store's end, mirroring
    tools/map_prep/pack_map.py's check_floor, because a hand-edited bundle skips
    the packer. A zero resolution collapses the console's bounds, and counts
    that do not fill the grid make the surveyed percentage meaningless.
    """
    where = "floor %d" % floor.index
    if not math.isfinite(floor.resolution_m) or floor.resolution_m <= 0:
        raise BundleError("%s: resolution_m is %r; it must be a positive number of metres per cell"
                          % (where, floor.resolution_m))
    width, height = floor.size_cells
    if width < 1 or height < 1:
        raise BundleError("%s: size_cells is %d x %d; both must be at least 1" % (where, width, height))
    counts = (("free", floor.free_cells), ("occupied", floor.occupied_cells), ("unknown", floor.unknown_cells))
    for label, count in counts:
        if count < 0:
            raise BundleError("%s: the %s cell count is negative (%d)" % (where, label, count))
    counted = sum(count for _, count in counts)
    if counted != width * height:
        raise BundleError("%s: the grid is %d x %d = %d cells, but free + occupied + unknown add up to %d"
                          % (where, width, height, width * height, counted))


def parse_pgm(data: bytes) -> Tuple[int, int, bytes]:
    """(width, height, pixels) of an 8-bit binary PGM. Raises BundleError.

    The pixels must be exactly width x height bytes: a short or long file is
    refused rather than trimmed into agreement."""
    match = _PGM_HEADER.match(data)
    if match is None:
        raise BundleError("the planning grid is not a binary PGM (P5)")
    width, height, maxval = (int(group) for group in match.groups())
    if maxval != 255:
        raise BundleError("the planning grid has maxval %d; only 8-bit (255) grids are handled" % maxval)
    pixels = data[match.end():]
    if len(pixels) != width * height:
        raise BundleError("the planning grid's header says %d x %d = %d cells, but %d bytes of pixels follow"
                          % (width, height, width * height, len(pixels)))
    return width, height, pixels


def count_cells(pixels: bytes) -> Tuple[int, int, int]:
    """(free, occupied, unknown) by Nav2's thresholds (see OCCUPIED_THRESH)."""
    classes = pixels.translate(_CELL_CLASS)
    return classes.count(b"f"), classes.count(b"o"), classes.count(b"u")


def planning_grid_for(floor: MapFloor, data: bytes,
                      clearance_m: float = DEFAULT_CLEARANCE_M) -> PlanningGrid:
    """The PlanningGrid that `data` (a PGM) is for `floor`. Raises BundleError
    if it is not a PGM, is not exactly the floor's size, or the clearance is not
    a distance the schema takes."""
    where = "floor %d" % floor.index
    if (isinstance(clearance_m, bool) or not isinstance(clearance_m, (int, float))
            or not math.isfinite(clearance_m) or not 0 < clearance_m < 2):
        raise BundleError("clearance_m is %r; it must be a distance between 0 and 2 m" % (clearance_m,))
    try:
        width, height, pixels = parse_pgm(data)
    except BundleError as exc:
        raise BundleError("%s: %s" % (where, exc)) from None
    if (width, height) != tuple(floor.size_cells):
        raise BundleError(
            "%s: the planning grid is %d x %d cells but the floor's grid is %d x %d; it must be made "
            "in exactly the floor's frame, origin, size and resolution"
            % (where, width, height, floor.size_cells[0], floor.size_cells[1]))
    free, occupied, unknown = count_cells(pixels)
    return PlanningGrid(free_cells=free, occupied_cells=occupied, unknown_cells=unknown,
                        clearance_m=float(clearance_m))


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `path` through a uniquely named temp file beside it and os.replace,
    so a reader sees the old file or the new one. The name is unique because a
    second process (the set_planning CLI) may be writing the same directory."""
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(staged, path)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def floor_content_hash(directory: Path, site_map: SiteMap, floor: MapFloor,
                       log: Callable[..., None] = logger.warning) -> Optional[str]:
    """The content hash of `floor` as stored under `directory`, or None (logged)
    when its files cannot be hashed. The planning grid is the one map.json
    describes: `floor.planning` None means none, whatever is on disk."""
    floor_dir = directory / ("floor%d" % floor.index)
    try:
        grid = grid_entry((floor_dir / ARTIFACT_FILES["grid"]).read_bytes(), floor.resolution_m,
                          floor.origin_xy_m, where="floor %d's grid" % floor.index)
        planning = None
        if floor.planning is not None:
            planning = grid_entry((floor_dir / PLANNING_FILE).read_bytes(), floor.resolution_m,
                                  floor.origin_xy_m, where="floor %d's planning grid" % floor.index)
        return content_hash(site_map.map_id, grid, planning, zone_entries(site_map.zones))
    except (OSError, MapHashError) as exc:
        log("map %s floor %d has no content hash: %s", site_map.map_id, floor.index, exc)
        return None


def with_content_hashes(directory: Path, site_map: SiteMap,
                        log: Callable[..., None] = logger.warning) -> SiteMap:
    """`site_map` with every floor's content_hash computed from `directory`."""
    return site_map.model_copy(update={"floors": [
        f.model_copy(update={"content_hash": floor_content_hash(directory, site_map, f, log)})
        for f in site_map.floors]})


class MapNotFound(LookupError):
    """No map by that id (or an id that cannot name one)."""


class FloorNotFound(MapNotFound):
    """The map exists but has no floor by that index. The message says so."""


class PlanningNotFound(MapNotFound):
    """The floor exists but has no planning grid. The message says so."""


class VersionNotFound(MapNotFound):
    """The map exists but keeps no version by that number. The message says so."""


def _move_dir(source: Path, target: Path) -> None:
    """os.replace for a map directory, retried briefly on PermissionError.

    On Windows a directory cannot be renamed while any file in it is open, and
    a background quality check (maps/quality.py) reads a map's files without
    the store's lock. Its reads are short, so waiting out one is enough; a
    handle held longer than ~2 s fails as before. Other errors are not retried."""
    delay = 0.02
    for attempt in range(12):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _link_or_copy(source: Path, target: Path) -> None:
    """Hard-link `source` at `target`, or copy it where links are refused (a
    filesystem without them, another volume). Safe because the store never
    rewrites a file in place: every write goes through a new file and
    os.replace, so a link's other name keeps the old bytes."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _link_tree(source: Path, target: Path, skip=()) -> None:
    """`source`'s files at `target`, recursively, by _link_or_copy. Names
    starting with "." (a write in progress) and names in `skip` are left out."""
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name.startswith(".") or child.name in skip:
            continue
        if child.is_dir():
            _link_tree(child, target / child.name)
        elif child.is_file():
            _link_or_copy(child, target / child.name)


def _read_json(path: Path) -> Optional[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_map_id(map_id: object) -> bool:
    return (isinstance(map_id, str) and bool(MAP_ID_RE.fullmatch(map_id))
            and not _WINDOWS_DEVICES.fullmatch(map_id))


class MapStore:
    def __init__(self, root, sweep: bool = True):
        """`sweep=False` for a second process beside a running server (the
        set_planning CLI): the sweep assumes one owner, and run beside a live
        server it would remove that server's in-progress upload, or put back a
        map it is midway through replacing."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if sweep:
            self._sweep_stale()

    def _sweep_stale(self) -> None:
        """Clear what a crash left behind. Assumes one process owns the store.

        Scratch, upload and deleted directories are just removed. A
        `.replaced-*` directory may hold the *only* copy of a map (the process
        died between moving it aside and moving the new one in), so its map is
        put back first unless a map by that id exists again.
        """
        # This runs when the store is built, which `api/app.py` does at import: a
        # failure here would be a backend that does not start, over housekeeping.
        # So nothing in it propagates -- each directory is handled on its own, and
        # what could not be handled is logged and left for the next start.
        try:
            names = os.listdir(self.root)
        except OSError:
            logger.exception("could not list the map store at %s", self.root)
            return
        for name in names:
            path = self.root / name
            try:
                if not path.is_dir():
                    continue
                if name.startswith(_OLD_PREFIX):
                    restored_all = True
                    for child in path.iterdir():
                        if not (valid_map_id(child.name) and not (self.root / child.name).exists()):
                            continue
                        try:
                            os.replace(child, self.root / child.name)
                            logger.warning("recovered map %s from an interrupted replace", child.name)
                        except OSError:
                            restored_all = False
                            logger.exception("could not recover map %s from %s; leaving it there",
                                             child.name, path)
                    # That directory may hold the only copy of a map: remove it
                    # only once everything in it has been put back.
                    if restored_all:
                        shutil.rmtree(path, ignore_errors=True)
                elif name.startswith((_SCRATCH_PREFIX, UPLOAD_PREFIX, _DELETED_PREFIX)):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                logger.exception("could not sweep %s", path)

    # --- reading -----------------------------------------------------------

    def _dir(self, map_id: object) -> Optional[Path]:
        return self.root / map_id if valid_map_id(map_id) else None  # type: ignore[operator]

    def get(self, map_id: str) -> Optional[SiteMap]:
        directory = self._dir(map_id)
        if directory is None:
            return None
        try:
            site_map = SiteMap.model_validate_json((directory / MAP_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:  # ValidationError is a ValueError
            logger.warning("skipping unreadable map %s: %s", map_id, exc)
            return None
        if site_map.map_id != map_id:
            logger.warning("skipping map directory %s: its map_id is %r", map_id, site_map.map_id)
            return None
        if any(f.content_hash is None for f in site_map.floors):
            # Stored before 0.0.20 (or unhashable): computed, not written back,
            # so a read never writes. The next write stores it.
            site_map = with_content_hashes(directory, site_map, log=logger.debug)
        return site_map

    def list(self) -> List[SiteMap]:
        maps = []
        for name in os.listdir(self.root):
            site_map = self.get(name)  # refuses scratch and stray names
            if site_map is not None:
                maps.append(site_map)
        maps.sort(key=lambda m: (m.updated_at, m.map_id), reverse=True)
        return maps

    def artifact_path(self, map_id: str, floor: int, kind: str) -> Optional[Path]:
        """The stored file for one floor's artifact (ARTIFACT_FILES), or None.

        Never raises for caller input: anything but a real map id, a
        non-negative integer floor and one of the three kinds is simply "no such
        file". The path is built from the validated id, the integer and the
        fixed table's file name -- nothing else from the caller reaches it.
        """
        directory = self._dir(map_id)
        if directory is None:
            return None
        if not isinstance(floor, int) or isinstance(floor, bool) or floor < 0:
            return None
        if not isinstance(kind, str):
            return None
        file_name = ARTIFACT_FILES.get(kind)
        if file_name is None:
            return None
        path = directory / ("floor%d" % floor) / file_name
        return path if path.is_file() else None

    # --- writing -----------------------------------------------------------

    def save_bundle(self, bundle_dir: Path, source: str = "upload",
                    detail: Optional[dict] = None) -> SiteMap:
        """Validate a bundle directory and store it, replacing any map with the
        same id. Raises BundleError (and stores nothing) if it is unusable.

        The bundle it replaces is kept as a version (VERSIONS above). `source`
        ("upload", "survey") and `detail` are recorded in the new version.json.

        The new map is assembled in a scratch directory beside the store and
        moved into place with os.replace, so a reader sees the old map or the
        new one, never a mixture. A directory cannot be renamed over a
        non-empty one, so an existing map is first moved aside: for that
        instant the id reads as absent -- never as half-replaced.
        """
        bundle_dir = Path(bundle_dir)
        site_map = self._read_bundle(bundle_dir)
        directory = self._dir(site_map.map_id)
        if directory is None:
            raise BundleError("map_id %r is not a valid map id" % (site_map.map_id,))

        scratch = Path(tempfile.mkdtemp(dir=self.root, prefix=_SCRATCH_PREFIX))
        try:
            planning: Dict[int, PlanningGrid] = {}
            for floor in site_map.floors:
                floor_dir = scratch / ("floor%d" % floor.index)
                floor_dir.mkdir()
                for kind, file_name in ARTIFACT_FILES.items():
                    artifact = bundle_dir / ("floor%d" % floor.index) / file_name
                    if not artifact.is_file():
                        if kind in _REQUIRED:
                            raise BundleError("floor %d has no %s" % (floor.index, file_name))
                        if kind == "planning" and floor.planning is not None:
                            raise BundleError("floor %d describes a planning grid but has no %s"
                                              % (floor.index, file_name))
                        continue
                    # The router refuses this on upload and on serving too; a
                    # direct caller (a seeding script) must not be able to store
                    # a map whose cloud can never be served.
                    if kind == "cloud" and artifact.stat().st_size > MAX_CLOUD_BYTES:
                        raise BundleError("floor %d's cloud exceeds %d bytes" % (floor.index, MAX_CLOUD_BYTES))
                    if kind in ("track", "survey") and artifact.stat().st_size > MAX_TRACK_BYTES:
                        raise BundleError("floor %d's %s exceeds %d bytes"
                                          % (floor.index, file_name, MAX_TRACK_BYTES))
                    if kind == "planning":
                        # Counted from the file, never taken from the manifest:
                        # only the clearance is the bundle's to say.
                        clearance = floor.planning.clearance_m if floor.planning else DEFAULT_CLEARANCE_M
                        planning[floor.index] = planning_grid_for(floor, artifact.read_bytes(), clearance)
                    shutil.copyfile(artifact, floor_dir / file_name)
            # A floor's planning grid is the bundle's or nothing. One stored on
            # the map being replaced is not carried over (unlike its zones): it
            # was made in the old survey's frame and says where the robot may
            # walk, so it must be made again from the new survey.
            site_map = site_map.model_copy(update={"floors": [
                f.model_copy(update={"planning": planning.get(f.index)}) for f in site_map.floors]})

            with self._lock:
                # The zones operators drew are not in a bundle, and when a map
                # was first made is not a property of its latest survey.
                # `updated_at` is the new bundle's.
                existing = self.get(site_map.map_id)
                # A replaced or new survey is always a draft, whatever the bundle
                # says. The zones from the old survey are kept (work is not thrown
                # away), but they were drawn in its frame and may now fall outside
                # the new extent or on a different floor; "active" is what
                # missions plan against and says a person has checked them.
                # Nobody has.
                site_map = site_map.model_copy(update={"status": "draft"})
                if existing is not None:
                    site_map = site_map.model_copy(update={
                        "created_at": existing.created_at,
                        "zones": existing.zones,
                    })
                # From the files just copied and the zones just kept; whatever
                # the bundle said is replaced.
                site_map = with_content_hashes(scratch, site_map)
                (scratch / MAP_FILE).write_text(site_map.model_dump_json(indent=2), encoding="utf-8")
                version = self._carry_versions(directory, scratch)
                self._write_version_file(scratch, {"version": version, "source": source,
                                                   "created_at": _now(), **(detail or {})})
                self._swap_in(directory, scratch)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        return site_map

    def _swap_in(self, directory: Path, scratch: Path) -> None:
        """Move `scratch` into place as `directory`. Call under the lock.

        A directory cannot be renamed over a non-empty one, so an existing map
        is first moved aside: for that instant the id reads as absent -- never
        as half-replaced."""
        aside = None
        try:
            if directory.exists():
                aside = Path(tempfile.mkdtemp(dir=self.root, prefix=_OLD_PREFIX))
                _move_dir(directory, aside / directory.name)
            _move_dir(scratch, directory)
        except BaseException:
            if aside is not None:
                if (aside / directory.name).exists():
                    # Put the old map back. If even that fails it stays in
                    # `aside`, which is not deleted, and the next start of
                    # the store restores it.
                    _move_dir(aside / directory.name, directory)
                shutil.rmtree(aside, ignore_errors=True)
            raise
        if aside is not None:
            shutil.rmtree(aside, ignore_errors=True)

    # --- versions ------------------------------------------------------------

    @staticmethod
    def _write_version_file(directory: Path, doc: dict) -> None:
        # A plain write: `directory` is a scratch directory nobody reads yet.
        (directory / VERSION_FILE).write_text(json.dumps(doc, indent=2), encoding="utf-8")

    @staticmethod
    def _archived(directory: Path) -> Dict[int, Path]:
        """version number -> its directory under versions/."""
        found: Dict[int, Path] = {}
        versions = directory / VERSIONS_DIR
        if not versions.is_dir():
            return found
        for child in versions.iterdir():
            match = _VERSION_DIR_RE.fullmatch(child.name)
            if match and (child / MAP_FILE).is_file():
                found[int(match.group(1))] = child
        return found

    def _current_version(self, directory: Path) -> int:
        """The live bundle's version number. A map stored before versions
        existed has no version.json and is version 1 (or one past the newest
        archive, should there be one)."""
        doc = _read_json(directory / VERSION_FILE) or {}
        number = doc.get("version")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            return number
        archived = self._archived(directory)
        return max(archived) + 1 if archived else 1

    def _carry_versions(self, directory: Path, scratch: Path) -> int:
        """Under the lock, before `scratch` replaces `directory`: link the
        archives `directory` keeps, and its live bundle as one more, into
        `scratch/versions/`, keeping the newest MAX_VERSIONS. Returns the
        number the new bundle gets: 1 for a map that is new."""
        if not (directory / MAP_FILE).is_file():
            return 1
        archived = self._archived(directory)
        current = self._current_version(directory)
        target = scratch / VERSIONS_DIR
        keep = sorted(set(archived) | {current})[-MAX_VERSIONS:]
        for number in keep:
            into = target / ("v%d" % number)
            if number in archived:
                _link_tree(archived[number], into)
            else:
                _link_tree(directory, into, skip=(VERSIONS_DIR,))
                if not (into / VERSION_FILE).is_file():
                    self._write_version_file(into, {"version": number, "source": "upload"})
        return max(keep) + 1

    def current_version(self, map_id: str) -> int:
        """The live bundle's version number. Raises MapNotFound."""
        directory = self._dir(map_id)
        if directory is None or not (directory / MAP_FILE).is_file():
            raise MapNotFound(map_id)
        return self._current_version(directory)

    def version_dir(self, map_id: str, version: int) -> Optional[Path]:
        """The directory of an archived version, or None. Never raises for
        caller input: the number is an int, so the name is built, not taken."""
        directory = self._dir(map_id)
        if directory is None or not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return None
        path = directory / VERSIONS_DIR / ("v%d" % version)
        return path if (path / MAP_FILE).is_file() else None

    def list_versions(self, map_id: str) -> List[dict]:
        """The live bundle and every kept version, newest first. Raises MapNotFound."""
        directory = self._dir(map_id)
        current = self.get(map_id)
        if directory is None or current is None:
            raise MapNotFound(map_id)
        entries = [self._version_entry(directory, current, self._current_version(directory), True)]
        for number, path in sorted(self._archived(directory).items(), reverse=True):
            try:
                site_map = SiteMap.model_validate_json((path / MAP_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("skipping unreadable version %d of map %s: %s", number, map_id, exc)
                continue
            entries.append(self._version_entry(path, site_map, number, False))
        return entries

    @staticmethod
    def _version_entry(path: Path, site_map: SiteMap, number: int, current: bool) -> dict:
        doc = _read_json(path / VERSION_FILE) or {}
        quality = _read_json(path / QUALITY_FILE)
        return {
            "version": number,
            "current": current,
            "source": doc.get("source", "upload"),
            "created_at": doc.get("created_at") or site_map.updated_at,
            "restored_from": doc.get("restored_from"),
            "detail": {k: v for k, v in doc.items()
                       if k not in ("version", "source", "created_at", "restored_from")},
            "name": site_map.name,
            "updated_at": site_map.updated_at,
            "zones": len(site_map.zones),
            "floors": [{"index": f.index, "size_cells": list(f.size_cells),
                        "resolution_m": f.resolution_m, "origin_xy_m": list(f.origin_xy_m),
                        "free_cells": f.free_cells, "occupied_cells": f.occupied_cells,
                        "unknown_cells": f.unknown_cells, "has_planning": f.planning is not None,
                        "has_track": (path / ("floor%d" % f.index) / ARTIFACT_FILES["track"]).is_file()}
                       for f in site_map.floors],
            "quality": None if quality is None else {
                "passed": quality.get("passed"), "computed_at": quality.get("computed_at"),
                "failed": quality.get("failed", [])},
        }

    def version_artifact_path(self, map_id: str, version: int, floor: int, kind: str) -> Optional[Path]:
        """artifact_path for an archived version, with the same refusals."""
        path = self.version_dir(map_id, version)
        if path is None or not isinstance(floor, int) or isinstance(floor, bool) or floor < 0:
            return None
        file_name = ARTIFACT_FILES.get(kind) if isinstance(kind, str) else None
        if file_name is None:
            return None
        found = path / ("floor%d" % floor) / file_name
        return found if found.is_file() else None

    def restore_version(self, map_id: str, version: int) -> SiteMap:
        """Make archived `version` the live bundle again, under a new number.

        Raises MapNotFound, or VersionNotFound when no such version is kept.
        The live bundle is archived first, as a re-upload archives it. The map
        keeps its current zones and creation date and drops to draft; the
        restored floors keep their own planning grids (same frame). It has no
        quality report until one runs on it: a report names the version it
        was made for, so the archived one would only read as stale."""
        directory = self._dir(map_id)
        if directory is None:
            raise MapNotFound(map_id)
        with self._lock:
            current = self.get(map_id)
            if current is None:
                raise MapNotFound(map_id)
            source = self.version_dir(map_id, version)
            if source is None:
                raise VersionNotFound("map %s keeps no version %r" % (map_id, version))
            try:
                archived = SiteMap.model_validate_json((source / MAP_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise BundleError("version %d of map %s is unreadable: %s" % (version, map_id, exc)) from None
            scratch = Path(tempfile.mkdtemp(dir=self.root, prefix=_SCRATCH_PREFIX))
            try:
                for floor in archived.floors:
                    floor_source = source / ("floor%d" % floor.index)
                    if not floor_source.is_dir():
                        raise BundleError("version %d of map %s has no files for floor %d"
                                          % (version, map_id, floor.index))
                    floor_target = scratch / ("floor%d" % floor.index)
                    floor_target.mkdir()
                    for file_name in ARTIFACT_FILES.values():
                        if (floor_source / file_name).is_file():
                            _link_or_copy(floor_source / file_name, floor_target / file_name)
                restored = archived.model_copy(update={
                    "zones": current.zones, "status": "draft",
                    "created_at": current.created_at, "updated_at": _now()})
                restored = with_content_hashes(scratch, restored)
                (scratch / MAP_FILE).write_text(restored.model_dump_json(indent=2), encoding="utf-8")
                number = self._carry_versions(directory, scratch)
                self._write_version_file(scratch, {"version": number, "source": "restore",
                                                   "restored_from": version, "created_at": _now()})
                self._swap_in(directory, scratch)
            except BaseException:
                shutil.rmtree(scratch, ignore_errors=True)
                raise
        return restored

    # --- the quality report (maps/quality.py computes it) ------------------

    def snapshot(self, map_id: str, kinds=("grid", "planning", "cloud", "track")
                 ) -> Tuple[SiteMap, int, Dict[int, Dict[str, Optional[bytes]]]]:
        """(map, version, {floor index: {kind: bytes or None}}), read under the
        lock so that no write lands halfway through -- and so the reader never
        holds a file open while a write renames it (Windows refuses that).
        Raises MapNotFound. A few tens of MB at most per floor."""
        directory = self._dir(map_id)
        if directory is None:
            raise MapNotFound(map_id)
        with self._lock:
            site_map = self.get(map_id)
            if site_map is None:
                raise MapNotFound(map_id)
            files: Dict[int, Dict[str, Optional[bytes]]] = {}
            for floor in site_map.floors:
                floor_dir = directory / ("floor%d" % floor.index)
                files[floor.index] = {}
                for kind in kinds:
                    path = floor_dir / ARTIFACT_FILES[kind]
                    files[floor.index][kind] = path.read_bytes() if path.is_file() else None
            return site_map, self._current_version(directory), files

    def read_quality(self, map_id: str) -> Optional[dict]:
        directory = self._dir(map_id)
        return None if directory is None else _read_json(directory / QUALITY_FILE)

    def write_quality(self, map_id: str, report: dict, expect_version: Optional[int] = None) -> bool:
        """Store `report` as the map's quality report. False (nothing written)
        when the map is gone or, given `expect_version`, has had its bundle
        replaced since the report started: a report on the old bundle must not
        land on the new one."""
        directory = self._dir(map_id)
        if directory is None:
            raise MapNotFound(map_id)
        with self._lock:
            if not (directory / MAP_FILE).is_file():
                return False
            if expect_version is not None and self._current_version(directory) != expect_version:
                return False
            _atomic_write(directory / QUALITY_FILE, json.dumps(report, indent=2).encode("utf-8"))
        return True

    # --- curating ------------------------------------------------------------

    def save_zones(self, map_id: str, zones: List[MapZone]) -> SiteMap:
        """Replace a map's zones with `zones`. Raises MapNotFound for an unknown
        map and ValueError for a list that cannot be stored (a zone belonging to
        another map, a repeated id, a non-finite vertex, too many)."""
        zones = list(zones)
        if len(zones) > MAX_ZONES:
            raise ValueError("a map holds at most %d zones" % MAX_ZONES)
        seen = set()
        for z in zones:
            if z.map_id != map_id:
                raise ValueError("zone %r belongs to map %r, not %r" % (z.zone_id, z.map_id, map_id))
            if z.zone_id in seen:
                raise ValueError("zone id %r is used twice" % (z.zone_id,))
            seen.add(z.zone_id)
            if len(z.polygon) > MAX_VERTICES_PER_ZONE:
                raise ValueError("zone %r has more than %d vertices" % (z.zone_id, MAX_VERTICES_PER_ZONE))
            if not all(math.isfinite(c) for point in z.polygon for c in point):
                raise ValueError("zone %r has a vertex that is not a finite number" % (z.zone_id,))
        return self._update(map_id, lambda current: {"zones": zones})

    def set_status(self, map_id: str, status: MapStatus) -> SiteMap:
        """Set a map's status. Raises MapNotFound for an unknown map and
        ValueError for a status that is not one, or for activating a map with no
        floors -- there would be nothing for a mission to plan on."""
        if status not in get_args(MapStatus):
            raise ValueError("%r is not a map status" % (status,))

        def change(current: SiteMap) -> dict:
            if status == "active" and not current.floors:
                raise ValueError("a map with no floors cannot be activated")
            return {"status": status}

        return self._update(map_id, change)

    def _update(self, map_id: str, change) -> SiteMap:
        """Re-read the map under the lock, apply `change(map) -> fields`, and
        write map.json atomically (a temp file in the same directory, then
        os.replace), so a reader sees the old file or the new one and a
        concurrent re-upload or other edit cannot be lost to a stale read."""
        directory = self._dir(map_id)
        if directory is None:
            raise MapNotFound(map_id)
        with self._lock:
            current = self.get(map_id)
            if current is None:
                raise MapNotFound(map_id)
            # Zones are in the hash; a status change leaves it as it was, and
            # recomputing then is what stores a pre-0.0.20 map's hash.
            updated = with_content_hashes(directory, current.model_copy(update=change(current)))
            self._write_map_file(directory, updated)
        return updated

    @staticmethod
    def _write_map_file(directory: Path, site_map: SiteMap) -> None:
        _atomic_write(directory / MAP_FILE, site_map.model_dump_json(indent=2).encode("utf-8"))

    # --- the planning grid ---------------------------------------------------

    def set_planning(self, map_id: str, floor_index: int, pgm: bytes,
                     clearance_m: float = DEFAULT_CLEARANCE_M) -> SiteMap:
        """Store `pgm` as floor `floor_index`'s planning grid and record its
        counts in that floor's `planning`, replacing any earlier one.

        Raises MapNotFound (or FloorNotFound) for a map or floor that is not
        there, and BundleError for bytes that are not a PGM of exactly the
        floor's size_cells, or a clearance the schema refuses.

        The map's status is left alone, on purpose. A re-upload drops a map to
        draft because it changes the frame its zones were drawn in; a planning
        grid is made in exactly that frame and only narrows where the robot may
        walk inside it, so nothing a reviewer checked has moved.
        """
        if len(pgm) > MAX_PLANNING_BYTES:
            raise BundleError("the planning grid exceeds %d bytes" % MAX_PLANNING_BYTES)

        def change(floor: MapFloor) -> Tuple[Optional[bytes], Optional[PlanningGrid]]:
            return pgm, planning_grid_for(floor, pgm, clearance_m)

        return self._swap_planning(map_id, floor_index, change)

    def delete_planning(self, map_id: str, floor_index: int) -> SiteMap:
        """Remove floor `floor_index`'s planning grid. Raises MapNotFound,
        FloorNotFound, or PlanningNotFound when the floor has none. Like
        set_planning, the map's status is left alone."""
        def change(floor: MapFloor) -> Tuple[Optional[bytes], Optional[PlanningGrid]]:
            return None, None

        return self._swap_planning(map_id, floor_index, change, must_exist=True)

    def _swap_planning(self, map_id: str, floor_index: int,
                       change: Callable[[MapFloor], Tuple[Optional[bytes], Optional[PlanningGrid]]],
                       must_exist: bool = False) -> SiteMap:
        """Under the lock, re-read the map, let `change(floor)` say the new file
        (None: none) and the new `planning`, then put both in place.

        The file and map.json are two writes, so the old file is moved aside
        first and put back if anything after it fails: a reader may see the old
        pair or the new pair, and never a planning.pgm that map.json does not
        describe once this returns or raises."""
        directory = self._dir(map_id)
        if directory is None:
            raise MapNotFound(map_id)
        if not isinstance(floor_index, int) or isinstance(floor_index, bool) or floor_index < 0:
            raise FloorNotFound("map %s has no floor %r" % (map_id, floor_index))
        with self._lock:
            current = self.get(map_id)
            if current is None:
                raise MapNotFound(map_id)
            floor = next((f for f in current.floors if f.index == floor_index), None)
            if floor is None:
                raise FloorNotFound("map %s has no floor %d" % (map_id, floor_index))
            floor_dir = directory / ("floor%d" % floor_index)
            target = floor_dir / PLANNING_FILE
            if must_exist and floor.planning is None and not target.is_file():
                raise PlanningNotFound("floor %d of map %s has no planning grid" % (floor_index, map_id))
            data, planning = change(floor)
            updated = current.model_copy(update={"floors": [
                f.model_copy(update={"planning": planning}) if f.index == floor_index else f
                for f in current.floors]})

            floor_dir.mkdir(exist_ok=True)
            aside: Optional[Path] = None
            placed = False
            try:
                if target.is_file():
                    fd, name = tempfile.mkstemp(dir=floor_dir, prefix="." + PLANNING_FILE + "-old-")
                    os.close(fd)
                    aside = Path(name)
                    os.replace(target, aside)
                if data is not None:
                    _atomic_write(target, data)
                    placed = True
                # The planning grid is in the hash: computed from the file now
                # in place (or its absence), before map.json says so.
                updated = with_content_hashes(directory, updated)
                self._write_map_file(directory, updated)
            except BaseException:
                try:
                    if placed:
                        target.unlink()
                    if aside is not None:
                        os.replace(aside, target)
                except OSError:
                    logger.exception("could not restore the planning grid of floor %d of map %s",
                                     floor_index, map_id)
                raise
            if aside is not None:
                try:
                    aside.unlink()
                except OSError:
                    logger.warning("could not remove %s", aside)
        return updated

    def delete(self, map_id: str) -> bool:
        directory = self._dir(map_id)
        if directory is None:
            return False
        with self._lock:
            if not directory.is_dir():
                return False
            # Move out of the namespace first, so a half-deleted map is never
            # visible, then remove.
            aside = Path(tempfile.mkdtemp(dir=self.root, prefix=_DELETED_PREFIX))
            _move_dir(directory, aside / directory.name)
        shutil.rmtree(aside, ignore_errors=True)
        return True

    @staticmethod
    def _read_bundle(bundle_dir: Path) -> SiteMap:
        try:
            doc = json.loads((bundle_dir / BUNDLE_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise BundleError("the bundle has no %s" % BUNDLE_FILE) from None
        except (OSError, ValueError) as exc:
            raise BundleError("%s is not readable JSON: %s" % (BUNDLE_FILE, exc)) from None
        try:
            # `floors` sits beside the `site_map` block in the bundle, not in it.
            site_map = SiteMap(**{**doc["site_map"], "floors": doc["floors"]})
        except (KeyError, TypeError) as exc:
            raise BundleError("%s is missing %s" % (BUNDLE_FILE, exc)) from None
        except ValidationError as exc:
            raise BundleError("%s is not a valid map: %s" % (BUNDLE_FILE, exc)) from None
        for floor in site_map.floors:
            _check_floor(floor)
        return site_map
