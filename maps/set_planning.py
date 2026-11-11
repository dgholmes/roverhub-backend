"""Set a floor's planning grid straight into a map store, without a browser.

    python -m maps.set_planning <maps_dir> <map_id> <floor_index> <file.pgm> [--clearance 0.2]

Run from roverhub-backend/ with its venv, e.g. to load the office's grid made by
tools/map_prep/planning_grid.py:

    python -m maps.set_planning data/maps map-office 0 office_planning.pgm

It goes through MapStore.set_planning, the same call PUT
/maps/{map_id}/floors/{index}/planning makes: the PGM must be exactly the
floor's size, the counts are Nav2's, the file and map.json are each replaced
atomically (a running server reading the map sees the old pair or the new one),
and the map's status is left as it was. The store is opened without its
start-up sweep, which assumes a single owner and would otherwise remove a
running server's in-progress upload.

Exit status: 0 when set, 2 when refused (no such map or floor, not a PGM of the
floor's size, a bad clearance, an unreadable file).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .store import DEFAULT_CLEARANCE_M, MAX_PLANNING_BYTES, BundleError, FloorNotFound, MapNotFound, MapStore


def _percent(part: int, whole: int) -> str:
    return "%5.1f%%" % (100.0 * part / whole) if whole else "  -  "


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m maps.set_planning",
                                     description="Set one floor's planning grid in a map store.")
    parser.add_argument("maps_dir", help="the store directory (the backend's MAPS_DIR, e.g. data/maps)")
    parser.add_argument("map_id")
    parser.add_argument("floor_index", type=int)
    parser.add_argument("pgm", help="the planning grid, a P5 PGM in exactly the floor's frame")
    parser.add_argument("--clearance", type=float, default=DEFAULT_CLEARANCE_M, metavar="M",
                        help="the planner's padded half-width in metres (default %(default)s)")
    args = parser.parse_args(argv)

    root = Path(args.maps_dir)
    if not root.is_dir():
        # MapStore would create it; a mistyped path must not become an empty store.
        print("refused: %s is not a directory" % root, file=sys.stderr)
        return 2
    source = Path(args.pgm)
    try:
        if source.stat().st_size > MAX_PLANNING_BYTES:
            print("refused: %s exceeds %d bytes" % (source, MAX_PLANNING_BYTES), file=sys.stderr)
            return 2
        data = source.read_bytes()
    except OSError as exc:
        print("refused: cannot read %s: %s" % (source, exc), file=sys.stderr)
        return 2

    store = MapStore(root, sweep=False)
    try:
        site_map = store.set_planning(args.map_id, args.floor_index, data, args.clearance)
    except FloorNotFound as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    except MapNotFound:
        print("refused: no map %s in %s" % (args.map_id, root), file=sys.stderr)
        return 2
    except BundleError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2

    floor = next(f for f in site_map.floors if f.index == args.floor_index)
    planning = floor.planning
    assert planning is not None
    width, height = floor.size_cells
    total = width * height
    print("%s floor %d: planning grid set (%d x %d cells, clearance %.3g m)"
          % (site_map.map_id, floor.index, width, height, planning.clearance_m))
    print("  free     %10d  %s" % (planning.free_cells, _percent(planning.free_cells, total)))
    print("  occupied %10d  %s" % (planning.occupied_cells, _percent(planning.occupied_cells, total)))
    print("  unknown  %10d  %s" % (planning.unknown_cells, _percent(planning.unknown_cells, total)))
    print("  map status: %s (unchanged)" % site_map.status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
