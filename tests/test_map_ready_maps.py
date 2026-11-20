"""Map ready checklist (schema 0.0.29), the backend's part of spec §4.5, §4.8
and §4.9:

- a new floor's extent comes from the points within AUTO_TRIM_MARGIN_M (30 m)
  of the walked track, so far stray returns (40-58 m out on the 2026-11-20
  15:08 survey, whose canvas was 58 m wide for an 11 m office) no longer
  stretch the canvas, while a mall atrium's walls 15-30 m out are kept;
- POST /maps/{id}/floors/{n}/trim re-crops a stored floor to the walked area
  plus TRIM_MARGIN_M, as a new draft version, and refuses to cut a zone;
- PATCH /maps/{id} renames the map and its floors' labels, outside the
  content hash;
- activating a map with no zones needs `zones_reviewed: true`, which records
  who said none were needed, and when.
"""
from __future__ import annotations

import json
import math
import os

import pytest

np = pytest.importorskip("numpy")

from auth.deps import Principal  # noqa: E402
from auth.policy import ROUTES  # noqa: E402
from maps import survey_math as sm  # noqa: E402
from maps.router import build_maps_router  # noqa: E402
from maps.store import MapStore  # noqa: E402
from maps.survey import SurveyRequest, process_survey  # noqa: E402
from maps.trim import AUTO_TRIM_MARGIN_M, TRIM_MARGIN_M, trim_floor  # noqa: E402
from shared.schemas.maps import MapZone  # noqa: E402
from tests.test_maps_pipeline import _pitch, room_cloud, room_track, track_bytes, write_pcd  # noqa: E402
from tests.test_site_setup import _app_as  # noqa: E402

SITE = "site-venue"
MAP = "map-wide"
SUPERVISOR = Principal(kind="user", id="sue")


# --- §4.5 automatic: a new floor ignores far strays ---------------------------


def test_the_trim_button_takes_ten_metres_and_a_new_survey_thirty():
    # Thirty at survey time: Mid-360 returns useful for localisation reach
    # about 30 m, and a shopping-mall atrium's walls stand 15-30 m from the
    # walked path. Ten for the Trim button, which a person presses.
    assert TRIM_MARGIN_M == 10.0
    assert AUTO_TRIM_MARGIN_M == 30.0


def _far_patch(x=50.0, points=2000, seed=3):
    """Returns 40 m past the room's far wall (x = 10), over 41 m from the
    track: through a window, off a car park. Dense enough to survive the
    converter's 0.1 % sparse-cell drop, which 50 lone points would not."""
    rng = np.random.default_rng(seed)
    xyz = np.column_stack([rng.uniform(x - 0.5, x + 0.5, points), rng.uniform(3.0, 4.0, points),
                           rng.uniform(-0.5, 2.0, points)])
    return xyz @ _pitch(5.0).T


def test_far_outliers_do_not_stretch_a_new_floor(tmp_path):
    store = MapStore(tmp_path / "maps")
    cloud = write_pcd(tmp_path / "room.pcd", np.vstack([room_cloud(), _far_patch()]))
    work = tmp_path / "work"
    work.mkdir()
    process_survey(store, SurveyRequest(map_id="map-room", site_id=SITE, name="Room"), cloud,
                   track_bytes(room_track()), work)
    site_map = store.get("map-room")
    floor = site_map.floors[0]
    track = sm.read_track_csv(store.artifact_path("map-room", 0, "track").read_bytes())
    lo = track[:, 1:3].min(axis=0) - AUTO_TRIM_MARGIN_M - floor.resolution_m
    hi = track[:, 1:3].max(axis=0) + AUTO_TRIM_MARGIN_M + floor.resolution_m
    assert site_map.extent_min[0] >= lo[0] and site_map.extent_min[1] >= lo[1], site_map.extent_min
    assert site_map.extent_max[0] <= hi[0] and site_map.extent_max[1] <= hi[1], site_map.extent_max
    # The room itself (0..10 x 0..8) is all there.
    assert site_map.extent_min[0] <= 0.0 and site_map.extent_max[0] >= 10.0
    survey = json.loads(store.artifact_path("map-room", 0, "survey").read_text())
    assert survey["grid"]["trim_margin_m"] == AUTO_TRIM_MARGIN_M
    assert survey["grid"]["points_beyond_margin"] >= 2000


def test_far_walls_of_a_wide_space_are_kept(tmp_path):
    # A 32 m hall walked only in its west end: the east wall (x = 32) stands
    # over 23 m from the track -- past the Trim button's 10 m, inside 30.
    store = MapStore(tmp_path / "maps")
    cloud = write_pcd(tmp_path / "hall.pcd", room_cloud(width=32.0))
    work = tmp_path / "work"
    work.mkdir()
    process_survey(store, SurveyRequest(map_id="map-hall", site_id=SITE, name="Hall"), cloud,
                   track_bytes(room_track()), work)
    site_map = store.get("map-hall")
    floor = site_map.floors[0]
    track = sm.read_track_csv(store.artifact_path("map-hall", 0, "track").read_bytes())
    assert 32.0 - track[:, 1].max() > 20.0
    assert site_map.extent_max[0] >= 32.0, site_map.extent_max
    _, _, pixels = _pgm(store.artifact_path("map-hall", 0, "grid").read_bytes())
    width, height = floor.size_cells
    image = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width)
    col = int(round((32.0 - floor.origin_xy_m[0]) / floor.resolution_m))
    assert (image[:, max(0, col - 2):col + 3] == 0).sum() > 50  # the east wall is drawn
    survey = json.loads(store.artifact_path("map-hall", 0, "survey").read_text())
    assert survey["grid"]["points_beyond_margin"] == 0


# --- §4.5 the Trim button ------------------------------------------------------

RES = 0.1
ORIGIN = (-44.0, -2.0)
SIZE = (600, 120)  # 60 m x 12 m: the room, and a speck 43 m off to the west


def _cell(x, y):
    col = int(round((x - ORIGIN[0]) / RES))
    row = SIZE[1] - 1 - int(round((y - ORIGIN[1]) / RES))
    return row, col


def _stretched_grid():
    image = np.full((SIZE[1], SIZE[0]), 205, dtype=np.uint8)
    r0, c0 = _cell(0.0, 8.0)
    r1, c1 = _cell(11.0, 0.0)
    image[r0:r1 + 1, c0:c1 + 1] = 254
    image[r0, c0:c1 + 1] = image[r1, c0:c1 + 1] = 0
    image[r0:r1 + 1, c0] = image[r0:r1 + 1, c1] = 0
    row, col = _cell(-43.0, 3.0)
    image[row, col] = 0
    return image


def _track():
    t = np.linspace(0, 2 * math.pi, 200)
    return np.column_stack([np.arange(200) * 0.1, 5.5 + 3.5 * np.cos(t), 4 + 2 * np.sin(t), np.zeros(200)])


def _blob(blob_id, x, y):
    return {"id": blob_id, "centroid": [x, y], "bbox": [x - 0.2, y - 0.2, x + 0.2, y + 0.2],
            "z_range": [0.0, 1.0], "area_m2": 0.16, "points": 4}


def _stretched_bundle(directory):
    """A one-floor map as the 15:08 survey left it: a 60 m canvas for an 11 m
    room, with a planning grid, a cloud sample, a moved layer and the track."""
    floor_dir = directory / "floor0"
    floor_dir.mkdir(parents=True)
    image = _stretched_grid()
    (floor_dir / "grid.pgm").write_bytes(sm.pgm_bytes(image))
    (floor_dir / "grid.png").write_bytes(sm.png_bytes(image))
    (floor_dir / "planning.pgm").write_bytes(sm.pgm_bytes(image))
    xs, ys = np.meshgrid(np.arange(0.5, 10.5, 0.5), np.arange(0.5, 7.5, 0.5))
    room = np.column_stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)])
    far = np.array([[-43.0, 3.0, 0.5], [-42.5, 3.5, 1.0]])
    (floor_dir / "cloud.bin").write_bytes(np.vstack([room, far]).astype("<f4").tobytes())
    (floor_dir / "track.csv").write_bytes(sm.track_csv_bytes(_track()))
    (floor_dir / "moved.json").write_text(json.dumps(
        {"version": 1, "blobs": [_blob("b-in", 4.0, 4.0), _blob("b-far", -40.0, 3.0)]}))
    free, occupied, unknown = (int((image == v).sum()) for v in (254, 0, 205))
    width, height = SIZE
    (directory / "site_map.json").write_text(json.dumps({
        "site_map": {"map_id": MAP, "site_id": SITE, "name": "Office", "source": "slam",
                     "extent_min": list(ORIGIN),
                     "extent_max": [ORIGIN[0] + width * RES, ORIGIN[1] + height * RES],
                     "created_at": "2026-11-20T06:08:51Z", "updated_at": "2026-11-20T06:08:51Z"},
        "floors": [{"index": 0, "label": "1F", "floor_z_m": 0.0, "resolution_m": RES,
                    "origin_xy_m": list(ORIGIN), "size_cells": [width, height],
                    "free_cells": free, "occupied_cells": occupied, "unknown_cells": unknown,
                    "planning": {"free_cells": free, "occupied_cells": occupied, "unknown_cells": unknown},
                    "grid": "floor0/grid.pgm", "preview": "floor0/grid.png"}],
    }))
    return directory


def _zone(zone_id, label, polygon):
    return MapZone(zone_id=zone_id, map_id=MAP, kind="no_go", label=label, polygon=[tuple(p) for p in polygon])


@pytest.fixture
def maps(tmp_path):
    """(router, client as a supervisor) with the stretched map stored."""
    router = build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"), start_jobs=False)
    router.store.save_bundle(_stretched_bundle(tmp_path / "bundle"))
    return router, _app_as(router, SUPERVISOR)


def _inside(points, floor):
    width, height = floor["size_cells"]
    x0, y0 = floor["origin_xy_m"]
    res = floor["resolution_m"]
    return bool(np.all((points[:, 0] >= x0) & (points[:, 0] <= x0 + width * res)
                       & (points[:, 1] >= y0) & (points[:, 1] <= y0 + height * res)))


def test_trim_recrops_to_the_walked_area_as_a_new_draft_version(maps):
    router, client = maps
    store = router.store
    store.set_status(MAP, "active")
    before = store.get(MAP)
    version = store.current_version(MAP)

    response = client.post("/maps/%s/floors/0/trim" % MAP)
    assert response.status_code == 200, response.text
    trimmed = response.json()
    floor = trimmed["floors"][0]
    assert store.current_version(MAP) == version + 1
    assert trimmed["status"] == "draft"
    assert floor["content_hash"] != before.floors[0].content_hash
    assert floor["label"] == "1F"

    # Track x 2..9, y 2..6 plus 10 m, inside the old 60 x 12 m canvas.
    assert floor["origin_xy_m"] == pytest.approx([-8.0, -2.0])
    assert floor["size_cells"] == [240, 120]
    assert trimmed["extent_min"] == pytest.approx([-8.0, -2.0])
    assert trimmed["extent_max"] == pytest.approx([16.0, 10.0])
    track = sm.read_track_csv(store.artifact_path(MAP, 0, "track").read_bytes())
    assert _inside(track[:, 1:3], floor)

    # Every file that is a grid is the new size; the room is where it was.
    width, height, pixels = _pgm(store.artifact_path(MAP, 0, "grid").read_bytes())
    assert (width, height) == (240, 120)
    grid = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width)
    assert grid[height - 1 - int(round((4.0 + 2.0) / RES)), int(round((4.0 + 8.0) / RES))] == 254
    assert floor["free_cells"] + floor["occupied_cells"] + floor["unknown_cells"] == 240 * 120
    assert floor["occupied_cells"] < before.floors[0].occupied_cells  # the speck is gone
    assert _pgm(store.artifact_path(MAP, 0, "planning").read_bytes())[:2] == (240, 120)
    assert sum(floor["planning"][k] for k in ("free_cells", "occupied_cells", "unknown_cells")) == 240 * 120
    cloud = np.frombuffer(store.artifact_path(MAP, 0, "cloud").read_bytes(), dtype="<f4").reshape(-1, 3)
    assert len(cloud) == 20 * 14 and _inside(cloud, floor)
    moved = json.loads(store.artifact_path(MAP, 0, "moved").read_text())
    assert [b["id"] for b in moved["blobs"]] == ["b-in"]

    # The replaced bundle is kept, restorable.
    assert [v["version"] for v in store.list_versions(MAP)][:2] == [version + 1, version]


def test_trim_keeps_zones_inside_and_refuses_when_a_zone_would_be_cut(maps):
    router, client = maps
    store = router.store
    store.save_zones(MAP, [_zone("z-in", "Stairwell", [(1, 1), (2, 1), (2, 2)])])
    response = client.post("/maps/%s/floors/0/trim" % MAP)
    assert response.status_code == 200, response.text
    assert [z["zone_id"] for z in response.json()["zones"]] == ["z-in"]

    # A zone over the far speck (re-surveyed wide again) would be cut off.
    router.store.save_bundle(_stretched_bundle(store.root.parent / "again"))
    store.save_zones(MAP, [_zone("z-in", "Stairwell", [(1, 1), (2, 1), (2, 2)]),
                           _zone("z-far", "Loading dock", [(-43, 2), (-41, 2), (-41, 4)])])
    version = store.current_version(MAP)
    refused = client.post("/maps/%s/floors/0/trim" % MAP)
    assert refused.status_code == 409, refused.text
    assert "zone Loading dock lies outside the trimmed floor" in refused.json()["detail"]
    assert store.current_version(MAP) == version
    assert store.get(MAP).floors[0].size_cells == SIZE


def test_trim_refuses_a_floor_with_no_track_and_an_unknown_floor(maps):
    router, client = maps
    assert client.post("/maps/%s/floors/3/trim" % MAP).status_code == 404
    assert client.post("/maps/map-nope/floors/0/trim").status_code == 404
    os.remove(router.store.artifact_path(MAP, 0, "track"))
    refused = client.post("/maps/%s/floors/0/trim" % MAP)
    assert refused.status_code == 409 and "walked track" in refused.json()["detail"]


def test_trimming_a_floor_already_inside_the_margin_changes_nothing(maps):
    router, _ = maps
    trim_floor(router.store, MAP, 0)
    version = router.store.current_version(MAP)
    again = trim_floor(router.store, MAP, 0)
    assert router.store.current_version(MAP) == version
    assert again.floors[0].size_cells == (240, 120)


def _pgm(data):
    from maps.store import parse_pgm
    return parse_pgm(data)


# --- §4.9 rename ---------------------------------------------------------------


def test_rename_map_and_floor_label_keeps_content_hash(maps):
    router, client = maps
    router.store.set_status(MAP, "active")
    before = router.store.get(MAP)
    version = router.store.current_version(MAP)
    response = client.patch("/maps/%s" % MAP, json={"name": "  Office, level 1 ", "floors": [{"index": 0, "label": "G"}]})
    assert response.status_code == 200, response.text
    renamed = response.json()
    assert renamed["name"] == "Office, level 1"
    assert renamed["floors"][0]["label"] == "G"
    assert renamed["floors"][0]["content_hash"] == before.floors[0].content_hash
    assert renamed["status"] == "active"  # a label is not something a robot runs on
    assert router.store.current_version(MAP) == version
    assert router.store.get(MAP).name == "Office, level 1"

    # A blank label clears it; the name alone may change.
    cleared = client.patch("/maps/%s" % MAP, json={"floors": [{"index": 0, "label": " "}]}).json()
    assert cleared["floors"][0]["label"] is None and cleared["name"] == "Office, level 1"
    assert client.patch("/maps/%s" % MAP, json={"name": "Office"}).json()["floors"][0]["label"] is None


@pytest.mark.parametrize("body, status, words", [
    ({"name": "  "}, 422, "a map needs a name"),
    ({"floors": [{"index": 0, "label": "x" * 41}]}, 422, "a floor label is at most 40 characters"),
    ({"floors": [{"index": 4, "label": "4F"}]}, 404, "has no floor 4"),
])
def test_rename_refuses_a_blank_name_a_long_label_and_a_missing_floor(maps, body, status, words):
    router, client = maps
    response = client.patch("/maps/%s" % MAP, json=body)
    assert response.status_code == status, response.text
    assert words in json.dumps(response.json()["detail"])
    assert router.store.get(MAP).name == "Office"
    assert client.patch("/maps/map-nope", json={"name": "x"}).status_code == 404


# --- §4.8 activate asks about zones --------------------------------------------


def test_activate_with_no_zones_needs_zones_reviewed(tmp_path):
    router = build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"), start_jobs=False)
    client = _app_as(router, SUPERVISOR)
    files = {"cloud": write_pcd(tmp_path / "room.pcd", room_cloud()), "track": track_bytes(room_track())}
    uploader = _app_as(router, None)  # sign-in off: a survey upload is the engineer's, not under test here
    response = uploader.post("/map-surveys", data={"site_id": SITE, "name": "Room", "map_id": "map-room"},
                           files={"cloud": ("room.pcd", files["cloud"].read_bytes(), "application/octet-stream"),
                                  "track": ("track.csv", files["track"], "text/csv")})
    assert response.status_code == 202
    router.jobs.run_pending()  # the survey, then its quality check (it passes)

    for body in ({"status": "active"}, {"status": "active", "zones_reviewed": False}):
        refused = client.put("/maps/map-room/status", json=body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "zones_unreviewed"
    assert router.store.get("map-room").status == "draft"

    frozen = client.put("/maps/map-room/status", json={"status": "active", "zones_reviewed": True,
                                                       "zones_reviewed_by": "someone-else"})
    assert frozen.status_code == 200, frozen.text
    stored = router.store.get("map-room")
    assert stored.status == "active"
    assert stored.zones_reviewed_by == "sue"  # the signed-in user, never the body
    assert stored.zones_reviewed_at is not None

    # Back to draft, a zone drawn: no question asked.
    assert client.put("/maps/map-room/status", json={"status": "draft"}).status_code == 200
    router.store.save_zones("map-room", [MapZone(zone_id="z", map_id="map-room", kind="no_go", label="Glass",
                                                 polygon=[(1, 1), (2, 1), (2, 2)])])
    assert client.put("/maps/map-room/status", json={"status": "active"}).status_code == 200


def test_a_resent_activate_does_not_restamp_the_zones_review(maps):
    router, _ = maps
    store = router.store
    first = store.set_status(MAP, "active", zones_reviewed=True, zones_reviewed_by="sue")
    assert first.zones_reviewed_by == "sue" and first.zones_reviewed_at is not None
    again = store.set_status(MAP, "active", zones_reviewed=True, zones_reviewed_by="sam")
    assert (again.zones_reviewed_at, again.zones_reviewed_by) == (first.zones_reviewed_at, "sue")
    assert store.get(MAP).zones_reviewed_by == "sue"


def test_activating_a_map_that_has_zones_does_not_stamp_a_review(maps):
    router, _ = maps
    store = router.store
    store.save_zones(MAP, [_zone("z-in", "Stairwell", [(1, 1), (2, 1), (2, 2)])])
    frozen = store.set_status(MAP, "active", zones_reviewed=True, zones_reviewed_by="sue")
    assert frozen.status == "active"
    assert frozen.zones_reviewed_at is None and frozen.zones_reviewed_by is None


def test_rename_is_audited_with_the_caller(tmp_path):
    from tests.test_site_setup import Audit

    audit = Audit()
    router = build_maps_router(str(tmp_path / "maps"), audit=audit, jobs_dir=str(tmp_path / "jobs"),
                               start_jobs=False)
    router.store.save_bundle(_stretched_bundle(tmp_path / "bundle"))
    client = _app_as(router, Principal(kind="device", id="rover-a", device_site=SITE))
    assert client.patch("/maps/%s" % MAP, json={"name": "Office 2"}).status_code == 200
    (row,) = audit.of("map.rename")
    assert row["detail"]["by_device"] == "rover-a" and row["robot_id"] == "rover-a"


def test_the_new_routes_have_policy_rules_for_editzones():
    for key in (("POST", "/maps/{map_id}/floors/{index}/trim"), ("PATCH", "/maps/{map_id}")):
        assert ROUTES[key].capabilities == ("editZones",), key
