"""The map editor's backend (Missions > Maps > Edit map, 2026-11-16).

- the "probably moved" layer per floor: kinds `moved` (moved.json) and
  `moved_cloud` (moved.pcd), carried in a bundle, served, versioned, and set by
  PUT /maps/{id}/floors/{n}/moved ("Keep");
- POST /maps/{id}/floors/{n}/cloud-edits: delete boxes and "Remove" of moved
  blobs, as one job and one new version, with the planning grid re-rasterised
  under the edited footprints only (maps/cloud_edit.py);
- the rules in auth/policy.py, and the survey upload carrying the layer.

The floor here is a 2 m x 2 m room at 5 cm (40 x 40 cells), floor at z = 0:
a chair, a desk leg, a wall along x = 1.9, a strip of unknown along x < 0.2
with an occupied pocket inside it, and a passer-by the moved layer lists.
"""
from __future__ import annotations

import json
import math
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

np = pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.policy import ROUTES, role_can  # noqa: E402
from auth.tokens import TokenIssuer  # noqa: E402
from auth.users import UserStore  # noqa: E402
from maps import cloud_edit as ce  # noqa: E402
from maps.router import build_maps_router  # noqa: E402
from maps.store import BundleError, MapStore, count_cells  # noqa: E402
from shared.schemas.maps import MapZone  # noqa: E402

RES, W, H = 0.05, 40, 40
FRAME = ce.Frame(RES, (0.0, 0.0), W, H)
STEP = 0.025
MAP = "map-room"


def block(x0, y0, x1, y1, z0, z1, step=STEP):
    """Points filling a box, kept off its faces so no point sits on a cell edge."""
    xs, ys, zs = (np.arange(a + step / 2, b, step) for a, b in ((x0, x1), (y0, y1), (z0, z1)))
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
    return np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])


FLOOR = np.column_stack([np.repeat(np.arange(W) * RES + RES / 2, H), np.tile(np.arange(H) * RES + RES / 2, W),
                         np.zeros(W * H)])
CHAIR = block(0.80, 0.80, 1.20, 1.20, 0.10, 0.80)
LEG = block(0.30, 0.30, 0.40, 0.40, 0.10, 0.60)
WALL = block(1.85, 0.00, 1.95, 2.00, 0.10, 1.50)
POCKET = block(0.05, 1.00, 0.15, 1.10, 0.10, 0.50)      # inside the unknown strip
PERSON = block(1.40, 0.40, 1.50, 0.50, 0.20, 1.60)
CLOUD = np.vstack([FLOOR, CHAIR, LEG, WALL, POCKET, PERSON])
UNKNOWN_COLS = 4                                         # x < 0.2


def cell_mask(x0, y0, x1, y1):
    mask = np.zeros((H, W), dtype=bool)
    r0, r1, c0, c1 = FRAME.rect_cells(x0, y0, x1, y1)
    mask[r0:r1 + 1, c0:c1 + 1] = True
    return mask


def planning_image():
    """What planning_grid.py's rule makes of CLOUD: occupied where a return
    lies in the band, free elsewhere, and the unknown strip."""
    hits = ce.band_hits(FRAME, CLOUD, 0.0, None, (0.05, 2.0))
    image = np.where(hits > 0, 0, 254).astype(np.uint8)
    strip = np.zeros((H, W), dtype=bool)
    strip[:, :UNKNOWN_COLS] = True
    image[strip & (hits == 0)] = 205
    return image


def pgm(image):
    return b"P5\n%d %d\n255\n" % (W, H) + image.tobytes()


def moved_doc():
    return {"version": 1, "source": "transient_filter 1", "params": {"voxel_m": 0.1},
            "blobs": [
                {"id": "b001", "centroid": [1.45, 0.45], "bbox": [1.4, 0.4, 1.5, 0.5], "z_range": [0.2, 1.6],
                 "area_m2": 0.01, "points": len(PERSON), "hits": 12, "pass_through": 30,
                 "polygon": [[1.4, 0.4], [1.5, 0.4], [1.5, 0.5], [1.4, 0.5]]},
                {"id": "b002", "centroid": [0.35, 0.35], "bbox": [0.3, 0.3, 0.4, 0.4], "z_range": [0.1, 0.6],
                 "area_m2": 0.01, "points": len(LEG), "hits": 9, "pass_through": 7,
                 "polygon": [[0.3, 0.3], [0.4, 0.3], [0.4, 0.4], [0.3, 0.4]]},
                {"id": "b003", "centroid": [0.6, 1.6], "bbox": [0.55, 1.55, 0.65, 1.65], "z_range": [0.3, 0.9],
                 "area_m2": 0.01, "points": 0, "hits": 3, "pass_through": 9, "polygon": []},
            ]}


MOVED_POINTS = np.vstack([PERSON, LEG])


def make_room_bundle(directory: Path, map_id=MAP, moved=True, moved_cloud=True, cloud=True, planning=True,
                     moved_bytes=None, moved_cloud_bytes=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    floor_dir = directory / "floor0"
    floor_dir.mkdir()
    image = planning_image()
    (floor_dir / "grid.pgm").write_bytes(pgm(image))
    (floor_dir / "grid.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    if cloud:
        (floor_dir / "cloud.bin").write_bytes(np.ascontiguousarray(CLOUD, dtype="<f4").tobytes())
    if planning:
        (floor_dir / "planning.pgm").write_bytes(pgm(image))
    if moved:
        (floor_dir / "moved.json").write_bytes(moved_bytes or json.dumps(moved_doc()).encode())
    if moved_cloud:
        (floor_dir / "moved.pcd").write_bytes(moved_cloud_bytes or ce.pcd_bytes(MOVED_POINTS))
    free, occupied, unknown = count_cells(image.tobytes())
    (directory / "site_map.json").write_text(json.dumps({
        "site_map": {"map_id": map_id, "site_id": "site-venue", "name": "Room", "source": "slam",
                     "extent_min": [0, 0], "extent_max": [2, 2],
                     "created_at": "2026-11-16T00:00:00Z", "updated_at": "2026-11-16T00:00:00Z"},
        "floors": [{"index": 0, "floor_z_m": 0.0, "resolution_m": RES, "origin_xy_m": [0.0, 0.0],
                    "size_cells": [W, H], "free_cells": free, "occupied_cells": occupied,
                    "unknown_cells": unknown}],
    }))
    return directory


class Audit:
    def __init__(self):
        self.rows = []

    def __call__(self, action, target, outcome, detail, **kw):
        self.rows.append({"action": action, "target": target, "outcome": outcome, "detail": detail, **kw})

    def of(self, action):
        return [r for r in self.rows if r["action"] == action]


@pytest.fixture
def audit():
    return Audit()


@pytest.fixture
def router(tmp_path, audit):
    router = build_maps_router(str(tmp_path / "maps"), audit=audit, jobs_dir=str(tmp_path / "jobs"),
                               start_jobs=False)
    router.store.save_bundle(make_room_bundle(tmp_path / "bundle"))
    return router


@pytest.fixture
def client(router):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def floor_files(router, kind, version=None):
    if version is None:
        path = router.store.artifact_path(MAP, 0, kind)
    else:
        path = router.store.version_artifact_path(MAP, version, 0, kind)
    return None if path is None else path.read_bytes()


def planning_now(router, version=None):
    data = floor_files(router, "planning", version)
    return np.frombuffer(data[len(b"P5\n40 40\n255\n"):], dtype=np.uint8).reshape(H, W)


def cloud_now(router, version=None):
    return np.frombuffer(floor_files(router, "cloud", version), dtype="<f4").reshape(-1, 3)


def edit(client, router, body, run=True):
    response = client.post("/maps/%s/floors/0/cloud-edits" % MAP, json=body)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["kind"] == "cloud_edit" and job["status"] == "queued"
    if run:
        router.jobs.run_pending()
        job = router.jobs.get(job["job_id"])
    return job


# --- the moved layer: stored, served, versioned --------------------------------


def test_a_bundle_carries_the_moved_layer_and_it_is_served(client, router):
    served = client.get("/maps/%s/floors/0/moved" % MAP)
    assert served.status_code == 200
    assert served.headers["content-type"] == "application/json"
    assert served.headers["cache-control"] == "no-cache"
    assert [b["id"] for b in served.json()["blobs"]] == ["b001", "b002", "b003"]
    cloud = client.get("/maps/%s/floors/0/moved_cloud" % MAP)
    assert cloud.status_code == 200 and cloud.headers["content-type"] == "application/octet-stream"
    assert len(ce.read_pcd_bytes(cloud.content, 10**6)) == len(MOVED_POINTS)
    assert client.get("/maps/%s/floors/1/moved" % MAP).status_code == 404
    assert client.get("/maps/%s/versions" % MAP).json()["items"][0]["floors"][0]["has_moved"] is True


def test_a_floor_without_the_layer_has_none(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_room_bundle(tmp_path / "b", moved=False, moved_cloud=False))
    assert store.artifact_path(MAP, 0, "moved") is None
    assert store.list_versions(MAP)[0]["floors"][0]["has_moved"] is False


def test_the_layer_is_uploaded_over_http_and_kept_in_versions(client, router, tmp_path):
    bundle = make_room_bundle(tmp_path / "up")
    files = {str(p.relative_to(bundle)).replace("\\", "/"): p.read_bytes()
             for p in sorted(bundle.rglob("*")) if p.is_file()}
    assert "floor0/moved.json" in files and "floor0/moved.pcd" in files
    response = client.post("/maps", files={n: (n.rsplit("/", 1)[-1], d, "application/octet-stream")
                                           for n, d in files.items()})
    assert response.status_code == 201, response.text
    # The upload replaced version 1, which keeps its layer.
    assert json.loads(floor_files(router, "moved", version=1))["blobs"][0]["id"] == "b001"
    served = client.get("/maps/%s/versions/1/floors/0/moved" % MAP)
    assert served.status_code == 200 and served.headers["content-type"] == "application/json"
    assert client.get("/maps/%s/versions/1/floors/0/moved_cloud" % MAP).status_code == 200
    # A restore brings it back as it was.
    router.store.set_moved(MAP, 0, json.dumps({**moved_doc(), "blobs": []}).encode())
    router.store.restore_version(MAP, 1)
    assert len(json.loads(floor_files(router, "moved"))["blobs"]) == 3


@pytest.mark.parametrize("moved, reason", [
    (b"not json", "not valid JSON"),
    (b'{"version": 2, "blobs": []}', "version"),
    (b'{"version": 1}', "blobs"),
    (json.dumps({"version": 1, "blobs": [{**moved_doc()["blobs"][0], "bbox": [2, 0, 1, 1]}]}).encode(), "bbox"),
    (json.dumps({"version": 1, "blobs": [{**moved_doc()["blobs"][0], "z_range": [1, 0]}]}).encode(), "z_range"),
    (b'{"version": 1, "blobs": [{"id": "b1", "centroid": [NaN, 0], "bbox": [0, 0, 1, 1], '
     b'"z_range": [0, 1], "area_m2": 1}]}', "NaN"),
    (json.dumps({"version": 1, "blobs": [moved_doc()["blobs"][0]] * 2}).encode(), "twice"),
])
def test_a_bundle_with_a_bad_moved_layer_is_refused(tmp_path, moved, reason):
    store = MapStore(tmp_path / "maps")
    with pytest.raises(BundleError, match=reason):
        store.save_bundle(make_room_bundle(tmp_path / "b", moved_bytes=moved))
    assert store.list() == []


def test_a_bundle_with_a_bad_moved_cloud_is_refused(tmp_path):
    store = MapStore(tmp_path / "maps")
    with pytest.raises(BundleError, match="moved.pcd"):
        store.save_bundle(make_room_bundle(tmp_path / "b", moved_cloud_bytes=b"not a pcd at all"))
    assert store.list() == []


def test_an_empty_moved_cloud_is_a_cloud(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_room_bundle(tmp_path / "b", moved_cloud_bytes=ce.pcd_bytes(np.zeros((0, 3)))))
    assert len(ce.read_pcd_bytes(store.artifact_path(MAP, 0, "moved_cloud").read_bytes(), 10)) == 0


# --- PUT moved: "Keep" --------------------------------------------------------


def test_keep_takes_the_blob_off_the_list_as_a_new_version(client, router, audit):
    router.store.set_status(MAP, "active")
    cloud_before, planning_before = cloud_now(router), planning_now(router)
    doc = moved_doc()
    doc["blobs"] = [b for b in doc["blobs"] if b["id"] != "b001"]
    response = client.put("/maps/%s/floors/0/moved" % MAP, content=json.dumps(doc))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2 and body["map"]["status"] == "draft"
    assert body["dropped"] == ["b001"] and body["added"] == [] and body["blobs"] == 2
    assert body["moved_cloud_points_removed"] == len(PERSON)
    assert router.jobs.get(body["quality_job_id"])["kind"] == "quality"
    assert [b["id"] for b in json.loads(floor_files(router, "moved"))["blobs"]] == ["b002", "b003"]
    assert len(ce.read_pcd_bytes(floor_files(router, "moved_cloud"), 10**6)) == len(LEG)
    # The cloud and the grids are untouched; the kept version still lists it.
    assert np.array_equal(cloud_now(router), cloud_before)
    assert np.array_equal(planning_now(router), planning_before)
    assert len(json.loads(floor_files(router, "moved", version=1))["blobs"]) == 3
    items = client.get("/maps/%s/versions" % MAP).json()["items"]
    assert (items[0]["version"], items[0]["source"]) == (2, "moved")
    (row,) = audit.of("map.moved.set")
    assert row["detail"]["dropped"] == 1 and row["detail"]["dropped_ids"] == ["b001"]
    assert row["site_id"] == "site-venue"


@pytest.mark.parametrize("body", [b"{", b'{"version": 1, "blobs": "x"}', b"[]"])
def test_put_moved_refuses_a_layer_that_does_not_parse(client, router, body):
    response = client.put("/maps/%s/floors/0/moved" % MAP, content=body)
    assert response.status_code == 400
    assert router.store.current_version(MAP) == 1


def test_put_moved_on_an_unknown_map_or_floor_is_404_and_a_big_one_413(client, monkeypatch):
    body = json.dumps(moved_doc())
    assert client.put("/maps/map-nope/floors/0/moved", content=body).status_code == 404
    assert client.put("/maps/%s/floors/3/moved" % MAP, content=body).status_code == 404
    monkeypatch.setattr("maps.router.MAX_MOVED_BYTES", 10)
    assert client.put("/maps/%s/floors/0/moved" % MAP, content=body).status_code == 413


# --- cloud edits ----------------------------------------------------------------


CHAIR_BOX = {"min": [0.75, 0.75, 0.05], "max": [1.25, 1.25, 0.9]}


def test_a_delete_box_removes_its_points_and_frees_only_the_cells_under_it(client, router, audit):
    router.store.set_status(MAP, "active")
    before = planning_now(router)
    job = edit(client, router, {"delete_boxes": [CHAIR_BOX], "reason": "a chair moved after the survey"})
    assert job["status"] == "succeeded", job["error"]
    result = job["result"]
    assert result["points_removed"] == len(CHAIR) == result["points_removed_per_box"][0]
    assert result["points_after"] == len(CLOUD) - len(CHAIR)
    assert result["version"] == 2 and result["previous_version"] == 1 and result["status"] == "draft"

    after = planning_now(router)
    footprint = cell_mask(0.75, 0.75, 1.25, 1.25)
    chair_cells = cell_mask(0.80, 0.80, 1.20, 1.20)
    assert (after[chair_cells] == 254).all() and (before[chair_cells] == 0).all()
    assert np.array_equal(after[~footprint], before[~footprint])     # nothing outside the box
    planning = result["planning"]
    assert planning["rerasterised"] and planning["freed_cells"] == int(chair_cells.sum())
    assert planning["parameters"]["source"].startswith("defaults")
    assert planning["parameters"]["low_band_m"] == [0.05, 0.9]
    assert planning["free_cells"] == int((after == 254).sum())

    # The cloud lost exactly the chair; the kept version did not.
    assert len(cloud_now(router)) == len(CLOUD) - len(CHAIR)
    assert len(cloud_now(router, version=1)) == len(CLOUD)
    assert np.array_equal(planning_now(router, version=1), before)

    site_map = router.store.get(MAP)
    assert site_map.status == "draft"
    floor = site_map.floors[0]
    assert (floor.planning.free_cells, floor.planning.occupied_cells, floor.planning.unknown_cells) == \
        count_cells(after.tobytes())
    version = client.get("/maps/%s/versions" % MAP).json()["items"][0]
    assert (version["version"], version["source"]) == (2, "cloud_edit")
    assert version["detail"]["points_removed"] == len(CHAIR) and version["detail"]["boxes"] == 1
    assert version["detail"]["reason"] == "a chair moved after the survey"

    quality = router.jobs.get(result["quality_job_id"])
    assert quality["kind"] == "quality" and quality["status"] == "succeeded"   # run_pending ran it too
    (row,) = audit.of("map.cloud_edit")
    assert row["detail"]["points_removed"] == len(CHAIR) and row["detail"]["cells_freed"] == planning["freed_cells"]
    assert row["detail"]["version"] == 2 and row["site_id"] == "site-venue"


def test_resolving_a_moved_blob_removes_its_points_and_its_entry(client, router):
    job = edit(client, router, {"resolve_moved": ["b002"]})
    assert job["status"] == "succeeded", job["error"]
    result = job["result"]
    assert result["points_removed"] == len(LEG)
    assert result["resolved_moved"] == ["b002"] and result["moved_blobs_left"] == 2
    assert result["moved_cloud_points_removed"] == len(LEG)
    assert [b["id"] for b in json.loads(floor_files(router, "moved"))["blobs"]] == ["b001", "b003"]
    assert len(ce.read_pcd_bytes(floor_files(router, "moved_cloud"), 10**6)) == len(PERSON)
    leg = cell_mask(0.30, 0.30, 0.40, 0.40)
    assert (planning_now(router)[leg] == 254).all()


def _router_with(tmp_path, audit, **bundle):
    router = build_maps_router(str(tmp_path / "maps"), audit=audit, start_jobs=False)
    router.store.save_bundle(make_room_bundle(tmp_path / "b", **bundle))
    app = FastAPI()
    app.include_router(router)
    return router, TestClient(app)


def test_remove_takes_only_the_blobs_moved_points_and_their_neighbours(tmp_path, audit):
    # A blob's bbox x z_range can hold still things too. Here moved.pcd says
    # only the leg's bottom moved (z < 0.3): its points within one voxel
    # (0.1 m) go, the rest of the leg -- still, in the same box -- stays.
    moved_part = LEG[LEG[:, 2] < 0.3] - [0, 0, 0.01]     # off the 2.5 cm lattice: no ties at one voxel
    router, client = _router_with(tmp_path, audit, moved_cloud_bytes=ce.pcd_bytes(moved_part))
    job = edit(client, router, {"resolve_moved": ["b002"]})
    assert job["status"] == "succeeded", job["error"]
    left = cloud_now(router)
    leg_left = left[(left[:, 0] > 0.3) & (left[:, 0] < 0.4) & (left[:, 1] > 0.3) & (left[:, 1] < 0.4)
                    & (left[:, 2] > 0.05)]
    top = moved_part[:, 2].max()
    # A neighbour of a moved point that moved.pcd did not list is removed ...
    assert not (np.abs(leg_left[:, 2] - (top + STEP)) < 1e-4).any()
    # ... a still point farther than a voxel from every moved point survives.
    assert (leg_left[:, 2] > top + 0.1 + 1e-6).all() and len(leg_left) > 0
    assert (np.abs(leg_left[:, 2] - LEG[:, 2].max()) < 1e-4).any()
    assert job["result"]["points_removed"] == len(LEG) - len(leg_left)
    assert job["result"]["planning"]["parameters"]["moved_match_m"] == 0.1
    # The still remainder is still an obstacle.
    leg = cell_mask(0.31, 0.31, 0.39, 0.39)
    assert (planning_now(router)[leg] == 0).all()
    assert job["result"]["planning"]["kept_returns_cells"] == int(leg.sum())


def test_remove_matches_within_the_layers_own_voxel(tmp_path, audit):
    doc = moved_doc()
    doc["params"]["voxel_m"] = 0.05
    moved_part = LEG[LEG[:, 2] < 0.3] - [0, 0, 0.01]     # off the 2.5 cm lattice: no ties at one voxel
    router, client = _router_with(tmp_path, audit, moved_bytes=json.dumps(doc).encode(),
                                  moved_cloud_bytes=ce.pcd_bytes(moved_part))
    job = edit(client, router, {"resolve_moved": ["b002"]})
    left = cloud_now(router)
    assert job["result"]["planning"]["parameters"]["moved_match_m"] == 0.05
    assert job["result"]["points_removed"] == int((LEG[:, 2] < moved_part[:, 2].max() + 0.05).sum())
    assert len(left) == len(CLOUD) - job["result"]["points_removed"]


def test_a_blobs_points_are_inside_its_polygon(tmp_path, audit):
    # A triangle over the leg: moved.pcd's points outside it are not the blob's.
    doc = moved_doc()
    doc["blobs"][1]["polygon"] = [[0.3, 0.3], [0.4, 0.3], [0.4, 0.4]]
    router, client = _router_with(tmp_path, audit, moved_bytes=json.dumps(doc).encode())
    job = edit(client, router, {"resolve_moved": ["b002"]})
    xy = LEG[:, :2] - [0.3, 0.3]
    inside = xy[:, 0] >= xy[:, 1]                       # on or below the diagonal
    assert job["result"]["moved_cloud_points_removed"] == int(inside.sum())
    # The rest stay on the amber layer (the floor cloud's points beside them go
    # anyway: they are within a voxel of the blob's own).
    assert len(ce.read_pcd_bytes(floor_files(router, "moved_cloud"), 10**6)) == len(PERSON) + int((~inside).sum())


def test_remove_needs_the_moved_points(tmp_path, audit):
    router, client = _router_with(tmp_path, audit, moved_cloud=False)
    response = client.post("/maps/%s/floors/0/cloud-edits" % MAP, json={"resolve_moved": ["b002"]})
    assert response.status_code == 400
    assert response.json()["detail"] == "this floor has no moved points; use a 3D delete box instead"
    # Keep and delete boxes do not need them.
    assert edit(client, router, {"keep_moved": ["b002"], "delete_boxes": [CHAIR_BOX]})["status"] == "succeeded"


def test_keep_through_cloud_edits_changes_no_cloud_and_no_grid(client, router):
    before_cloud, before_grid = cloud_now(router), planning_now(router)
    job = edit(client, router, {"resolve_moved": ["b002"], "keep_moved": ["b001"]})
    result = job["result"]
    assert result["kept_moved"] == ["b001"] and result["moved_blobs_left"] == 1
    assert result["points_removed"] == len(LEG)               # b001's points stay in the cloud
    assert len(cloud_now(router)) == len(before_cloud) - len(LEG)
    person = cell_mask(1.40, 0.40, 1.50, 0.50)
    assert np.array_equal(planning_now(router)[person], before_grid[person])
    assert len(ce.read_pcd_bytes(floor_files(router, "moved_cloud"), 10**6)) == 0
    assert router.store.current_version(MAP) == 2               # one version for both


def test_a_box_that_cuts_only_part_of_a_thing_leaves_its_cells(client, router):
    before = planning_now(router)
    job = edit(client, router, {"delete_boxes": [{"min": [0.75, 0.75, 0.05], "max": [1.25, 1.25, 0.5]}]})
    planning = job["result"]["planning"]
    assert planning["freed_cells"] == 0
    assert planning["kept_returns_cells"] == int(cell_mask(0.80, 0.80, 1.20, 1.20).sum())
    assert np.array_equal(planning_now(router), before)


def test_never_frees_unknown_space_or_a_pocket_the_walkable_area_cannot_reach(client, router):
    before = planning_now(router)
    box = {"min": [0.0, 0.9, -0.5], "max": [0.2, 1.2, 3.0]}          # the unknown strip, pocket and all
    job = edit(client, router, {"delete_boxes": [box]})
    planning = job["result"]["planning"]
    after = planning_now(router)
    pocket = cell_mask(0.05, 1.00, 0.15, 1.10)
    assert (after[pocket] == 0).all()                               # occupied, but nowhere to join
    assert planning["kept_unconnected_cells"] == int(pocket.sum()) and planning["freed_cells"] == 0
    assert (after[before == 205] == 205).all()                      # unknown stays unknown


def test_zones_win_over_a_cloud_edit(client, router):
    router.store.save_zones(MAP, [
        MapZone(zone_id="nogo", map_id=MAP, kind="no_go", label="Cables",
                polygon=[(1.0, 1.0), (1.3, 1.0), (1.3, 1.3), (1.0, 1.3)]),
    ])
    job = edit(client, router, {"delete_boxes": [CHAIR_BOX]})
    after = planning_now(router)
    inside = cell_mask(1.0, 1.0, 1.2, 1.2)
    assert (after[inside] == 0).all()
    assert job["result"]["planning"]["kept_zone_cells"] == int(inside.sum())
    assert (after[cell_mask(0.80, 0.80, 0.95, 0.95)] == 254).all()
    assert [z.zone_id for z in router.store.get(MAP).zones] == ["nogo"]


def test_outside_every_geofence_nothing_is_freed(client, router):
    router.store.save_zones(MAP, [
        MapZone(zone_id="fence", map_id=MAP, kind="geofence", label="Room",
                polygon=[(0.0, 0.0), (1.0, 0.0), (1.0, 2.0), (0.0, 2.0)]),
    ])
    edit(client, router, {"delete_boxes": [CHAIR_BOX]})
    after = planning_now(router)
    assert (after[cell_mask(0.80, 0.80, 0.95, 1.20)] == 254).all()     # inside the fence (x < 1.0)
    assert (after[cell_mask(1.05, 0.80, 1.20, 1.20)] == 0).all()


def test_a_cell_with_no_evidence_is_kept_unless_a_box_covers_it_whole(router, client):
    # An occupied cell with no sample point in it (the stored cloud is a sample):
    # removing a blob over it is no evidence; a box over the whole band is.
    image = planning_image()
    ghost = cell_mask(0.61, 1.56, 0.64, 1.59)                       # b003's spot; no cloud point
    image[ghost] = 0
    router.store.edit_planning(MAP, 0, pgm(image))
    job = edit(client, router, {"resolve_moved": ["b003"]})
    assert job["result"]["planning"]["kept_no_evidence_cells"] == 1
    assert (planning_now(router)[ghost] == 0).all()
    job = edit(client, router, {"delete_boxes": [{"min": [0.55, 1.50, -0.5], "max": [0.70, 1.65, 3.0]}]})
    assert job["result"]["planning"]["freed_cells"] == 1
    assert (planning_now(router)[ghost] == 254).all()
    # A box short of the band's top is not "the whole band".
    image[ghost] = 0
    router.store.edit_planning(MAP, 0, pgm(image))
    job = edit(client, router, {"delete_boxes": [{"min": [0.55, 1.50, -0.5], "max": [0.70, 1.65, 1.0]}]})
    assert job["result"]["planning"]["kept_no_evidence_cells"] == 1


def test_a_floor_without_a_planning_grid_still_loses_the_points(tmp_path, audit):
    router = build_maps_router(str(tmp_path / "maps"), audit=audit, start_jobs=False)
    router.store.save_bundle(make_room_bundle(tmp_path / "b", planning=False))
    app = FastAPI()
    app.include_router(router)
    job = edit(TestClient(app), router, {"delete_boxes": [CHAIR_BOX]})
    assert job["status"] == "succeeded"
    assert job["result"]["planning"]["rerasterised"] is False
    assert job["result"]["planning"]["reason"] == "this floor has no planning grid"
    assert router.store.artifact_path(MAP, 0, "planning") is None


def test_an_edit_that_lost_a_race_fails_and_stores_nothing(client, router, monkeypatch):
    # Another save lands between the job's read and its write.
    read = router.store.snapshot

    def read_then_someone_saves(*args, **kwargs):
        found = read(*args, **kwargs)
        router.store.edit_planning(MAP, 0, pgm(planning_image()))
        return found

    monkeypatch.setattr(router.store, "snapshot", read_then_someone_saves)
    job = edit(client, router, {"delete_boxes": [CHAIR_BOX]})
    assert job["status"] == "failed" and "changed while this edit" in job["error"]
    monkeypatch.undo()
    assert router.store.current_version(MAP) == 2
    assert len(cloud_now(router)) == len(CLOUD)


@pytest.mark.parametrize("body, reason", [
    ({"delete_boxes": [{"min": [1, 0, 0], "max": [0, 1, 1]}]}, "min is above max"),
    ({"delete_boxes": [{"min": [0, 0], "max": [1, 1, 1]}]}, "three finite numbers"),
    ({"delete_boxes": [{"min": [0, 0, 0], "max": [1, 1, "1"]}]}, "three finite numbers"),
    ({"delete_boxes": [{"min": [0, 0, 0], "max": [1, 1, 1], "pad": 1}]}, "must be"),
    ({"delete_boxes": [CHAIR_BOX] * (ce.MAX_DELETE_BOXES + 1)}, "at most 500"),
    ({"delete_boxes": "everything"}, "must be a list"),
    ({"resolve_moved": ["b999"]}, "no blob"),
    ({"resolve_moved": ["b001"], "keep_moved": ["b001"]}, "both removed and kept"),
    ({"resolve_moved": ["b001", "b001"]}, "twice"),
    ({}, "nothing to do"),
    ({"delete_boxes": [], "erase": True}, "unknown field"),
    ({"delete_boxes": [CHAIR_BOX], "reason": "x" * 501}, "reason"),
])
def test_bad_edits_are_refused_before_a_job_is_made(client, router, body, reason):
    response = client.post("/maps/%s/floors/0/cloud-edits" % MAP, json=body)
    assert response.status_code == 400, response.text
    assert reason in response.json()["detail"]
    assert router.jobs.list() == []


def test_a_non_finite_corner_is_refused(client, router):
    raw = b'{"delete_boxes": [{"min": [0, 0, 0], "max": [1, 1, Infinity]}]}'
    response = client.post("/maps/%s/floors/0/cloud-edits" % MAP, content=raw)
    assert response.status_code == 400 and "Infinity" in response.json()["detail"]


def test_cloud_edits_need_a_map_a_floor_and_a_cloud(client, router, tmp_path):
    body = {"delete_boxes": [CHAIR_BOX]}
    assert client.post("/maps/map-nope/floors/0/cloud-edits", json=body).status_code == 404
    assert client.post("/maps/%s/floors/2/cloud-edits" % MAP, json=body).status_code == 404
    router.store.save_bundle(make_room_bundle(tmp_path / "nc", map_id="map-bare", cloud=False, moved=False))
    response = client.post("/maps/map-bare/floors/0/cloud-edits", json=body)
    assert response.status_code == 400 and "no cloud" in response.json()["detail"]
    response = client.post("/maps/map-bare/floors/0/cloud-edits", json={"keep_moved": ["b001"]})
    assert response.status_code == 400 and "no moved layer" in response.json()["detail"]


def test_a_cloud_edit_body_over_the_cap_is_413(client, monkeypatch):
    monkeypatch.setattr(ce, "MAX_CLOUD_EDIT_BODY_BYTES", 16)
    assert client.post("/maps/%s/floors/0/cloud-edits" % MAP, json={"delete_boxes": [CHAIR_BOX]}).status_code == 413


def test_jobs_list_the_edit_with_its_request(client, router):
    job = edit(client, router, {"delete_boxes": [CHAIR_BOX], "resolve_moved": ["b002"]}, run=False)
    listed = client.get("/map-jobs?map_id=%s" % MAP).json()["items"]
    assert listed[0]["job_id"] == job["job_id"]
    assert listed[0]["detail"] == {"floor": 0, "boxes": 1, "resolve_moved": ["b002"], "keep_moved": [],
                                   "reason": None}


# --- the port of planning_grid.py's rule ------------------------------------------


def _planning_grid_module():
    here = Path(__file__).resolve()
    candidates = [os.environ.get("ROVERHUB_MAP_PREP"), here.parents[2] / "tools" / "map_prep",
                  here.parents[3] / "roverhub_v0" / "tools" / "map_prep"]
    for candidate in candidates:
        if candidate and (Path(candidate) / "planning_grid.py").is_file():
            sys.path.insert(0, str(candidate))
            try:
                import planning_grid
            finally:
                sys.path.remove(str(candidate))
            return planning_grid
    pytest.skip("tools/map_prep/planning_grid.py is not checked out beside this repo (ROVERHUB_MAP_PREP)")


def _tilted_cloud(seed=3):
    """A 4 m room on a plane tilted 0.3 deg, with low clutter and stray returns."""
    rng = np.random.default_rng(seed)
    a, b, c = math.tan(math.radians(0.3)), -0.002, -0.4
    xy = rng.uniform(0, 4, (60_000, 2))
    floor = np.column_stack([xy, a * xy[:, 0] + b * xy[:, 1] + c + rng.normal(0, 0.01, len(xy))])
    clutter_xy = rng.uniform(0, 4, (4_000, 2))
    clutter = np.column_stack([clutter_xy, a * clutter_xy[:, 0] + b * clutter_xy[:, 1] + c
                               + rng.uniform(-0.1, 1.2, len(clutter_xy))])
    return np.vstack([floor, clutter]), {"a": a, "b": b, "c": c}


def test_the_band_rule_matches_planning_grid_py_on_a_synthetic_cloud():
    pg = _planning_grid_module()
    cloud, plane = _tilted_cloud()
    frame = pg.Frame(0.05, (0.0, 0.0), 80, 80)
    mine = ce.Frame(0.05, (0.0, 0.0), 80, 80)
    rows, cols, inside = pg.Frame.cells(frame, cloud[:, :2])
    my_rows, my_cols, my_inside = mine.cells(cloud[:, :2])
    assert np.array_equal(rows, my_rows) and np.array_equal(cols, my_cols) and np.array_equal(inside, my_inside)
    for use_plane in (plane, None):
        height = cloud[:, 2] - pg.floor_height(cloud, -0.4, use_plane)
        theirs = pg._cell_counts(frame, cloud[(height >= 0.05) & (height <= 0.9)])
        ours = ce.band_hits(mine, cloud, -0.4, use_plane, (0.05, 0.9))
        assert np.array_equal(theirs, ours)
    # And as build() uses it: its low-obstacle cells are ours at --min-hits 3.
    args = pg.parse_args(["x.pcd", "t.csv", "--like", "x.yaml", "--out", "o"])
    track = np.array([[0.0, 2.0, 2.0, 0.05], [1.0, 2.1, 2.0, 0.05], [2.0, 2.2, 2.1, 0.05]])
    _, stats, _ = pg.build(frame, np.zeros((80, 80), dtype=bool), cloud, -0.4, track, [], args, plane)
    assert stats["low_obstacle_cells"] == int((ce.band_hits(mine, cloud, -0.4, plane, (0.05, 0.9)) >= 3).sum())
    assert (args.low_band, args.min_hits) == (ce.DEFAULT_LOW_BAND_M, ce.DEFAULT_MIN_HITS)
    # The zone rules are planning_grid.py's.
    assert ce.ZONE_RULES == pg.ZONE_RULES


def test_the_band_rule_without_planning_grid_py():
    # A 3-point column at 0.5 m and a floor point: one cell, three hits.
    cloud = np.array([[0.12, 0.12, 0.5], [0.13, 0.13, 0.5], [0.14, 0.11, 0.5], [0.12, 0.12, 0.0],
                      [0.12, 0.12, 0.95]])
    hits = ce.band_hits(ce.Frame(0.05, (0.0, 0.0), 4, 4), cloud, 0.0, None, (0.05, 0.9))
    assert hits.sum() == 3 and hits[4 - 1 - 2, 2] == 3       # row counted down from max y


# --- the survey upload carries the layer, levelled with the cloud --------------------


def test_the_survey_levels_the_moved_layer_with_the_cloud(tmp_path):
    from maps.survey import SurveyRequest, process_survey
    from tests.test_maps_pipeline import room_cloud, room_track, write_pcd

    raw = room_cloud(pitch_deg=5.0)
    cloud_path = tmp_path / "cloud.pcd"
    write_pcd(cloud_path, raw)
    track = room_track(pitch_deg=5.0)
    track_bytes = ("t,x,y,z\n" + "".join("%.3f,%.4f,%.4f,%.4f\n" % tuple(r) for r in track)).encode()
    # A blob 5 m along x at 0.5 m above the raw floor: the pitch lifts it.
    t = math.radians(5.0)
    pitch = np.array([[math.cos(t), 0, math.sin(t)], [0, 1, 0], [-math.sin(t), 0, math.cos(t)]])
    blob_points = np.array([[5.0, 4.0, 0.0], [5.1, 4.1, 0.1]]) @ pitch.T
    doc = {"version": 1, "blobs": [{"id": "b001", "centroid": blob_points[:, :2].mean(0).tolist(),
                                    "bbox": [*blob_points[:, :2].min(0).tolist(), *blob_points[:, :2].max(0).tolist()],
                                    "z_range": [float(blob_points[:, 2].min()), float(blob_points[:, 2].max())],
                                    "area_m2": 0.01, "points": 2, "polygon": []}]}
    (tmp_path / "moved.json").write_text(json.dumps(doc))
    (tmp_path / "moved.pcd").write_bytes(ce.pcd_bytes(blob_points))
    store = MapStore(tmp_path / "maps")
    result = process_survey(store, SurveyRequest(map_id=MAP, site_id="site-venue", name="Room"), cloud_path,
                            track_bytes, tmp_path, moved_path=tmp_path / "moved.json",
                            moved_cloud_path=tmp_path / "moved.pcd")
    assert result["levelled"] and result["moved_blobs"] == 1
    stored = json.loads(store.artifact_path(MAP, 0, "moved").read_bytes())
    blob = stored["blobs"][0]
    assert stored["params"]["levelled_by_server_deg"] == pytest.approx(5.0, abs=0.1)
    points = ce.read_pcd_bytes(store.artifact_path(MAP, 0, "moved_cloud").read_bytes(), 10)
    # Levelled, the points are 0.5 m and 0.6 m above the floor again ...
    assert points[1, 2] - points[0, 2] == pytest.approx(0.1, abs=0.02)
    floor_z = store.get(MAP).floors[0].floor_z_m
    assert points[0, 2] - floor_z == pytest.approx(0.5, abs=0.05)
    # ... and the blob's box still holds them.
    assert blob["bbox"][0] <= points[:, 0].min() and points[:, 0].max() <= blob["bbox"][2]
    assert blob["z_range"][0] <= points[:, 2].min() and points[:, 2].max() <= blob["z_range"][1]


def test_the_survey_endpoint_takes_the_layer_parts(tmp_path):
    from tests.test_maps_pipeline import room_cloud, room_track, write_pcd

    router = build_maps_router(str(tmp_path / "maps"), start_jobs=False)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    write_pcd(tmp_path / "cloud.pcd", room_cloud(pitch_deg=0.0))
    track = room_track(pitch_deg=0.0)
    track_csv = "t,x,y,z\n" + "".join("%.3f,%.4f,%.4f,%.4f\n" % tuple(r) for r in track)
    response = client.post("/map-surveys", data={"site_id": "site-venue", "name": "Room", "map_id": MAP},
                           files={"cloud": ("c.pcd", (tmp_path / "cloud.pcd").read_bytes()),
                                  "track": ("t.csv", track_csv.encode()),
                                  "moved": ("moved.json", json.dumps(moved_doc()).encode()),
                                  "moved_cloud": ("moved.pcd", ce.pcd_bytes(MOVED_POINTS))})
    assert response.status_code == 202, response.text
    assert response.json()["detail"]["moved"] is True
    router.jobs.run_pending()
    job = router.jobs.get(response.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["moved_blobs"] == 3
    assert client.get("/maps/%s/floors/0/moved" % MAP).json()["blobs"][0]["id"] == "b001"


# --- access rules -------------------------------------------------------------------


EDITOR_ROUTES = [("PUT", "/maps/{map_id}/floors/{index}/moved"),
                 ("POST", "/maps/{map_id}/floors/{index}/cloud-edits"),
                 ("PUT", "/maps/{map_id}/floors/{index}/planning")]


def test_the_editor_routes_have_rules_and_are_on_the_app():
    app_routes = {(m, r.path) for r in backend.app.routes if isinstance(r, APIRoute) for m in r.methods}
    for key in EDITOR_ROUTES:
        assert key in app_routes, key
        capabilities = ROUTES[key].capabilities
        for role in ("supervisor", "engineer"):
            assert any(role_can(role, c) for c in capabilities), (key, role)
        for role in ("viewer", "operator", "technician"):
            assert not any(role_can(role, c) for c in capabilities), (key, role)


PASSWORD = "correct horse"


@pytest.fixture
def auth_on(tmp_path, monkeypatch):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret-long-enough-for-hs256-ok"))
    for username, role in (("vic", "viewer"), ("ana", "operator"), ("tom", "technician"),
                           ("eve", "engineer"), ("sue", "supervisor")):
        auth.users.create(username=username, display_name=username, password=PASSWORD,
                          org_role="member", site_roles={"site-venue": role})
    return auth


@pytest.fixture
def app_map(tmp_path):
    map_id = "map-editor-" + uuid.uuid4().hex[:8]
    backend.maps_router.store.save_bundle(make_room_bundle(tmp_path / "app-bundle", map_id=map_id))
    yield map_id
    backend.maps_router.store.delete(map_id)


def _bearer(client, username):
    token = client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_with_sign_in_on_only_a_supervisor_or_engineer_edits(auth_on, app_map):
    client = TestClient(backend.app)
    anonymous = TestClient(backend.app)
    vic, ana, tom, eve, sue = (_bearer(client, n) for n in ("vic", "ana", "tom", "eve", "sue"))
    moved_url = "/maps/%s/floors/0/moved" % app_map
    edits_url = "/maps/%s/floors/0/cloud-edits" % app_map
    body = json.dumps(moved_doc())
    # Refused before anything is looked at: no job, no version.
    assert anonymous.put(moved_url, content=body).status_code == 401
    assert anonymous.post(edits_url, json={"keep_moved": ["b001"]}).status_code == 401
    for who in (vic, ana, tom):
        assert client.put(moved_url, content=body, headers=who).status_code == 403
        assert client.post(edits_url, json={"keep_moved": ["b001"]}, headers=who).status_code == 403
    assert backend.maps_router.store.current_version(app_map) == 1
    for who in (eve, sue):
        assert client.put(moved_url, content=body, headers=who).status_code == 200
    accepted = client.post(edits_url, json={"keep_moved": ["b001"]}, headers=eve)
    assert accepted.status_code == 202 and accepted.json()["requested_by"] is not None
    backend.maps_router.jobs.wait(accepted.json()["job_id"], timeout=30)
    # Everyone at the site reads the layer.
    assert client.get(moved_url, headers=vic).status_code == 200


# --- the localisation grid (grid.pgm, the slice AMCL matches) -------------------


def grid_now(router):
    data = floor_files(router, "grid")
    return np.frombuffer(data[len(b"P5\n40 40\n255\n"):], dtype=np.uint8).reshape(H, W)


def test_a_removed_passer_by_leaves_the_localisation_grid_and_a_wall_beside_it_stays(client, router):
    before_grid = grid_now(router)
    before = router.store.get(MAP).floors[0]
    wall_slab = {"min": [1.84, 0.35, 1.2], "max": [1.96, 0.55, 1.4]}   # the wall goes on up to 1.5 m
    job = edit(client, router, {"resolve_moved": ["b001"], "delete_boxes": [wall_slab]})
    assert job["status"] == "succeeded", job["error"]
    loc = job["result"]["localisation"]
    person = cell_mask(1.41, 0.41, 1.49, 0.49)
    wall = cell_mask(1.86, 0.36, 1.94, 0.54)
    after = grid_now(router)
    assert (before_grid[person] == 0).all() and (after[person] == 254).all()
    assert (after[wall] == 0).all()
    assert loc["rerasterised"] and loc["reason"] is None
    assert loc["freed_cells"] == int(person.sum()) and loc["kept_returns_cells"] >= int(wall.sum())
    assert loc["parameters"]["obstacle_band_m"] == [1.2, 2.0] and loc["parameters"]["source"].startswith("default")
    changed = after != before_grid
    assert np.array_equal(changed, person)                              # nothing else moved
    floor = router.store.get(MAP).floors[0]
    assert (floor.free_cells, floor.occupied_cells, floor.unknown_cells) == count_cells(after.tobytes())
    # The grid is in the content hash: the device needs the new files.
    assert floor.content_hash == job["result"]["content_hash"] != before.content_hash
    assert floor_files(router, "preview") != floor_files(router, "preview", version=1)   # the png follows


def test_nothing_changes_in_the_localisation_grid_without_structure_band_points(client, router):
    before = grid_now(router)
    job = edit(client, router, {"delete_boxes": [CHAIR_BOX]})          # the chair stops at 0.8 m
    loc = job["result"]["localisation"]
    assert loc["freed_cells"] == 0
    assert loc["kept_no_evidence_cells"] == int(cell_mask(0.81, 0.81, 1.19, 1.19).sum())
    assert np.array_equal(grid_now(router), before)
    assert floor_files(router, "preview") == floor_files(router, "preview", version=1)
    # The planning grid still lost the chair.
    assert job["result"]["planning"]["freed_cells"] > 0


def test_the_localisation_band_comes_from_survey_json_when_there_is_one():
    assert ce.localisation_band({"grid": {"obstacle_band_m": [0.15, 1.9]}}) == \
        ((0.15, 1.9), "survey.json grid.obstacle_band_m")
    assert ce.localisation_band(None)[0] == (1.2, 2.0)
    assert ce.localisation_band({"grid": {"obstacle_band_m": [2, 1]}})[0] == (1.2, 2.0)
