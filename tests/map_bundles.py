"""Hand-built map bundles in the layout tools/map_prep/pack_map.py writes.

Built here rather than by calling the packer: the backend's tests must not need
numpy or the converter, and what they pin is the *layout*, which is a contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

PGM = b"P5\n4 2\n255\n" + bytes([254, 254, 0, 205, 254, 0, 205, 205])
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
CLOUD = b"\x00" * 24  # two float32 x,y,z points


def floor_doc(index: int) -> dict:
    return {
        "index": index, "floor_z_m": -0.34 + 3.0 * index, "resolution_m": 0.05,
        "origin_xy_m": [-5.4753, -17.8671], "size_cells": [4, 2],
        "free_cells": 3, "occupied_cells": 2, "unknown_cells": 3,
        "grid": "floor%d/grid.pgm" % index, "preview": "floor%d/grid.png" % index,
    }


def make_bundle(directory: Path, map_id: str = "map-venue", floors: int = 1,
                cloud: bool = False, name: str = "Venue", site_id: str = "site-venue") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    docs = [floor_doc(i) for i in range(floors)]
    for i in range(floors):
        floor_dir = directory / ("floor%d" % i)
        floor_dir.mkdir()
        (floor_dir / "grid.pgm").write_bytes(PGM)
        (floor_dir / "grid.png").write_bytes(PNG)
        if cloud:
            (floor_dir / "cloud.bin").write_bytes(CLOUD)
            docs[i]["cloud"] = "floor%d/cloud.bin" % i
    (directory / "site_map.json").write_text(json.dumps({
        "site_map": {
            "map_id": map_id, "site_id": site_id, "name": name, "source": "slam",
            "extent_min": [-5.4753, -17.8671], "extent_max": [-5.2753, -17.7671],
            "created_at": "2026-11-08T00:00:00Z", "updated_at": "2026-11-08T00:00:00Z",
        },
        "floors": docs,
        "provenance": {"generated_by": "tools/map_prep/pack_map.py"},
    }))
    return directory


def bundle_parts(directory: Path) -> Dict[str, bytes]:
    """The bundle as the upload sends it: bundle-relative path -> bytes."""
    return {str(p.relative_to(directory)).replace("\\", "/"): p.read_bytes()
            for p in sorted(directory.rglob("*")) if p.is_file()}
