"""File-backed site maps.

A survey is packed into a *bundle* directory by tools/map_prep/pack_map.py:

    site_map.json       {"site_map": SiteMap fields, "floors": [MapFloor fields
                         + artifact names], "provenance": ...}
    floor<N>/grid.pgm   the occupancy grid
    floor<N>/grid.png   the same pixels, for a browser
    floor<N>/cloud.bin  float32 x,y,z triples, if a cloud was packed

`save_bundle` takes such a directory and keeps it under `<root>/<map_id>/` as

    map.json            the validated SiteMap (floors joined in)
    floor<N>/...        the three artifacts, byte for byte

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
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

from pydantic import ValidationError

from shared.schemas.maps import SiteMap

logger = logging.getLogger("roverhub.maps")

DEFAULT_MAPS_DIR = str(Path(__file__).resolve().parents[1] / "data" / "maps")

# Mirrors MAX_CLOUD_BYTES in tools/map_prep/pack_map.py, which refuses a larger
# cloud at packing time. The router enforces it again on upload and on serving,
# so the two figures must stay the same expression: 8_000_000 here would let a
# cloud pass packing and fail serving by 388,608 bytes.
MAX_CLOUD_BYTES = 8 * 1024 * 1024

BUNDLE_FILE = "site_map.json"
MAP_FILE = "map.json"

# kind (as it appears in a URL) -> file name inside floor<N>/. The whole of what
# a caller may ask for.
ARTIFACT_FILES = {"grid": "grid.pgm", "preview": "grid.png", "cloud": "cloud.bin"}
_REQUIRED = ("grid", "preview")

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


class BundleError(ValueError):
    """The bundle is not a usable map. The message is safe to show the uploader."""


def valid_map_id(map_id: object) -> bool:
    return (isinstance(map_id, str) and bool(MAP_ID_RE.fullmatch(map_id))
            and not _WINDOWS_DEVICES.fullmatch(map_id))


class MapStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sweep_stale()

    def _sweep_stale(self) -> None:
        """Clear what a crash left behind. Assumes one process owns the store.

        Scratch, upload and deleted directories are just removed. A
        `.replaced-*` directory may hold the *only* copy of a map (the process
        died between moving it aside and moving the new one in), so its map is
        put back first unless a map by that id exists again.
        """
        for name in os.listdir(self.root):
            path = self.root / name
            if not path.is_dir():
                continue
            if name.startswith(_OLD_PREFIX):
                for child in path.iterdir():
                    if valid_map_id(child.name) and not (self.root / child.name).exists():
                        os.replace(child, self.root / child.name)
                        logger.warning("recovered map %s from an interrupted replace", child.name)
                shutil.rmtree(path, ignore_errors=True)
            elif name.startswith((_SCRATCH_PREFIX, UPLOAD_PREFIX, _DELETED_PREFIX)):
                shutil.rmtree(path, ignore_errors=True)

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
        """The stored file for one floor's grid, preview or cloud, or None.

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

    def save_bundle(self, bundle_dir: Path) -> SiteMap:
        """Validate a bundle directory and store it, replacing any map with the
        same id. Raises BundleError (and stores nothing) if it is unusable.

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
            for floor in site_map.floors:
                floor_dir = scratch / ("floor%d" % floor.index)
                floor_dir.mkdir()
                for kind, file_name in ARTIFACT_FILES.items():
                    source = bundle_dir / ("floor%d" % floor.index) / file_name
                    if not source.is_file():
                        if kind in _REQUIRED:
                            raise BundleError("floor %d has no %s" % (floor.index, file_name))
                        continue
                    # The router refuses this on upload and on serving too; a
                    # direct caller (a seeding script) must not be able to store
                    # a map whose cloud can never be served.
                    if kind == "cloud" and source.stat().st_size > MAX_CLOUD_BYTES:
                        raise BundleError("floor %d's cloud exceeds %d bytes" % (floor.index, MAX_CLOUD_BYTES))
                    shutil.copyfile(source, floor_dir / file_name)

            with self._lock:
                # What operators have done to a map is not in a bundle, and when
                # it was first made is not a property of its latest survey.
                # `updated_at` is the new bundle's.
                existing = self.get(site_map.map_id)
                if existing is not None:
                    site_map = site_map.model_copy(update={
                        "created_at": existing.created_at,
                        "status": existing.status,
                        "zones": existing.zones,
                    })
                (scratch / MAP_FILE).write_text(site_map.model_dump_json(indent=2), encoding="utf-8")

                aside = None
                try:
                    if directory.exists():
                        aside = Path(tempfile.mkdtemp(dir=self.root, prefix=_OLD_PREFIX))
                        os.replace(directory, aside / directory.name)
                    os.replace(scratch, directory)
                except BaseException:
                    if aside is not None:
                        if (aside / directory.name).exists():
                            # Put the old map back. If even that fails it stays
                            # in `aside`, which is not deleted, and the next
                            # start of the store restores it.
                            os.replace(aside / directory.name, directory)
                        shutil.rmtree(aside, ignore_errors=True)
                    raise
                if aside is not None:
                    shutil.rmtree(aside, ignore_errors=True)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        return site_map

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
            os.replace(directory, aside / directory.name)
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
            return SiteMap(**{**doc["site_map"], "floors": doc["floors"]})
        except (KeyError, TypeError) as exc:
            raise BundleError("%s is missing %s" % (BUNDLE_FILE, exc)) from None
        except ValidationError as exc:
            raise BundleError("%s is not a valid map: %s" % (BUNDLE_FILE, exc)) from None
