"""Every stored floor carries a content hash (tracker 1-5-01), and it is the
number an edge device reaches from its own copies of the same files
(shared/schemas/map_hash.py, robot_bridge/map_integrity.py)."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maps.router import build_maps_router
from maps.store import MAP_FILE, MapStore
from shared.schemas.map_hash import hash_device_files, is_map_hash
from shared.schemas.maps import MapZone
from tests.map_bundles import PGM, make_bundle

PLANNING = b"P5\n4 2\n255\n" + bytes([254, 254, 0, 205, 254, 254, 205, 205])


def zone(zone_id="z1", polygon=((0, 0), (1, 0), (1, 1)), kind="no_go", label="Stairwell"):
    return MapZone(zone_id=zone_id, map_id="map-venue", kind=kind, label=label, polygon=list(polygon))


@pytest.fixture
def store(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b", floors=2))
    return store


def _hashes(store):
    return [f.content_hash for f in store.get("map-venue").floors]


def test_a_stored_bundle_has_a_hash_per_floor(store, tmp_path):
    first, second = _hashes(store)
    assert is_map_hash(first) and is_map_hash(second)
    # Two floors with identical grids and placement hash alike: the floor's
    # index is not part of the content.
    assert first == second
    stored = json.loads((tmp_path / "maps" / "map-venue" / MAP_FILE).read_text(encoding="utf-8"))
    assert stored["floors"][0]["content_hash"] == first


def test_a_hash_in_the_bundle_is_ignored(tmp_path):
    bundle = make_bundle(tmp_path / "b")
    doc = json.loads((bundle / "site_map.json").read_text())
    doc["floors"][0]["content_hash"] = "0" * 64
    (bundle / "site_map.json").write_text(json.dumps(doc))
    saved = MapStore(tmp_path / "maps").save_bundle(bundle)
    assert saved.floors[0].content_hash != "0" * 64


def test_zones_change_the_hash_and_a_label_does_not(store):
    before = _hashes(store)
    store.save_zones("map-venue", [zone()])
    with_zone = _hashes(store)
    assert with_zone[0] != before[0]
    store.save_zones("map-venue", [zone(label="Renamed")])
    assert _hashes(store) == with_zone
    store.save_zones("map-venue", [zone(polygon=((0, 0), (1, 0), (1, 2)))])
    assert _hashes(store)[0] not in (before[0], with_zone[0])
    store.save_zones("map-venue", [])
    assert _hashes(store) == before


def test_status_leaves_the_hash_alone(store):
    before = _hashes(store)
    store.set_status("map-venue", "active")
    assert _hashes(store) == before


def test_the_planning_grid_changes_only_its_floors_hash(store):
    before = _hashes(store)
    store.set_planning("map-venue", 1, PLANNING)
    after = _hashes(store)
    assert after[0] == before[0] and after[1] != before[1]
    store.delete_planning("map-venue", 1)
    assert _hashes(store) == before


def test_a_map_stored_without_a_hash_gets_one_on_read_without_a_write(store, tmp_path):
    path = tmp_path / "maps" / "map-venue" / MAP_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    expected = [f["content_hash"] for f in doc["floors"]]
    for f in doc["floors"]:
        del f["content_hash"]
    path.write_text(json.dumps(doc), encoding="utf-8")
    written = path.read_bytes()
    assert _hashes(store) == expected
    assert path.read_bytes() == written


def test_an_unhashable_floor_reads_as_none(store, tmp_path):
    (tmp_path / "maps" / "map-venue" / "floor1" / "grid.pgm").write_bytes(b"not a pgm")
    store.save_zones("map-venue", [zone()])
    first, second = _hashes(store)
    assert is_map_hash(first) and second is None


def test_the_device_reaches_the_same_number(store, tmp_path):
    """The operator's procedure end to end: the console's floor hash equals
    what the device computes from the files it was given (a map_saver header,
    the planning grid's own yaml and planning_grid.py's zones.json)."""
    store.save_zones("map-venue", [zone(), zone("z0", ((-1, -1), (2, -1), (2, 2), (-1, 2)), "geofence")])
    store.set_planning("map-venue", 0, PLANNING)
    console = _hashes(store)[0]

    device = tmp_path / "device"
    device.mkdir()
    header, pixels = PGM.split(b"255\n", 1)
    (device / "survey.pgm").write_bytes(b"P5\n# CREATOR: map_saver.cpp 0.050 m/pix\n4 2\n255\n" + pixels)
    (device / "survey.yaml").write_text(
        "image: survey.pgm\nresolution: 0.05\norigin: [-5.4753, -17.8671, 0.0]\nnegate: 0\n"
        "occupied_thresh: 0.65\nfree_thresh: 0.196\n")
    (device / "plan.pgm").write_bytes(PLANNING)
    (device / "plan.yaml").write_text("image: plan.pgm\nresolution: 0.05\norigin: [-5.4753, -17.8671, 0.0]\n")
    zones = [{"zone_id": z.zone_id, "kind": z.kind, "label": z.label, "polygon": [list(p) for p in z.polygon]}
             for z in store.get("map-venue").zones]
    (device / "zones.json").write_text(json.dumps({"version": 1, "map_id": "map-venue", "zones": zones}))

    assert hash_device_files("map-venue", str(device / "survey.yaml"), str(device / "plan.yaml"),
                             str(device / "zones.json")) == console


def test_get_map_exposes_it(tmp_path):
    router = build_maps_router(str(tmp_path / "maps"))
    router.store.save_bundle(make_bundle(tmp_path / "b"))
    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/maps/map-venue").json()
    assert is_map_hash(body["floors"][0]["content_hash"])
