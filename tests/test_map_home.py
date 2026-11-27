"""A home position per map floor (schema 0.0.33) and RETURN checked against it.

- maps/home.py: a proposed home against the floor's planning grid (never-walked
  cells count as blocked) and the map's zones;
- PUT / DELETE /maps/{id}/floors/{i}/home: stored on the floor, stamped by the
  server, audited, never a new version, never a drop to draft;
- POST /robots/{id}/commands RETURN: the pose must be the stored home.

Spec: docs/superpowers/specs/2026-11-26-resume-after-estop-and-return-home-design.md
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.policy import ROUTES
from control.commands import CommandStore
from control.router import RETURN_SCHEMA_VERSION, build_router
from maps.home import HOME_CLEARANCE_M, check_home
from maps.router import build_maps_router
from maps.store import MapStore, count_cells
from robots.registry import RobotRegistry
from shared.schemas.maps import MapFloor, MapZone
from shared.schemas.version import SCHEMA_VERSION

RES, W, H = 0.05, 40, 40          # a 2 m x 2 m floor, origin (0, 0)
FREE, OCCUPIED, UNKNOWN = 254, 0, 205
MAP_ID = "map-home"
SITE = "site-venue"
# The centre of cell (20, 20): 0.9 m from every edge of the grid.
HX, HY = 1.025, 1.025


def _floor(index=0) -> MapFloor:
    return MapFloor(index=index, floor_z_m=0.0, resolution_m=RES, origin_xy_m=(0.0, 0.0), size_cells=(W, H),
                    free_cells=W * H, occupied_cells=0, unknown_cells=0)


def _image(marks=()) -> bytearray:
    """All free, then each (col, j, value): j counts up from the bottom (+y),
    as a map frame does; the PGM's first row is the top (planning_grid.py)."""
    image = bytearray([FREE] * (W * H))
    for col, j, value in marks:
        image[(H - 1 - j) * W + col] = value
    return image


def _pgm(image) -> bytes:
    return b"P5\n%d %d\n255\n" % (W, H) + bytes(image)


def _zone(kind, polygon, label="Plant room", zone_id="z1"):
    extra = {}
    if kind == "stairs":
        extra["stairs"] = {"bottom_xy_m": (0.0, 0.0), "top_xy_m": (2.0, 2.0)}
    return MapZone(zone_id=zone_id, map_id=MAP_ID, kind=kind, label=label, polygon=polygon, **extra)


def _square(cx, cy, half):
    return [(cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half), (cx - half, cy + half)]


# --- maps/home.py: the check ---------------------------------------------------


def test_the_clearance_is_the_bridges_close_threshold():
    assert HOME_CLEARANCE_M == 0.45


def test_a_free_cell_with_room_to_turn_is_a_home():
    assert check_home(_floor(), _pgm(_image()), [], HX, HY) is None


@pytest.mark.parametrize("value", [OCCUPIED, UNKNOWN])
def test_a_home_on_a_cell_that_is_not_walked_is_outside_the_walked_area(value):
    code, message = check_home(_floor(), _pgm(_image([(20, 20, value)])), [], HX, HY)
    assert code == "home_not_clear"
    assert "outside the walked area" in message


def test_a_home_off_the_grid_is_outside_the_walked_area():
    code, message = check_home(_floor(), _pgm(_image()), [], 5.0, 1.0)
    assert (code, "outside the walked area" in message) == ("home_not_clear", True)


@pytest.mark.parametrize("value", [OCCUPIED, UNKNOWN])
def test_a_wall_or_unwalked_cell_0_4_m_away_leaves_no_room_to_turn(value):
    # Cell 28's centre is 8 cells = 0.4 m from home's.
    code, message = check_home(_floor(), _pgm(_image([(28, 20, value)])), [], HX, HY)
    assert code == "home_not_clear"
    assert "0.45 m" in message and "outside the walked area" not in message


def test_a_wall_0_5_m_away_is_room_enough():
    assert check_home(_floor(), _pgm(_image([(30, 20, OCCUPIED), (20, 10, UNKNOWN)])), [], HX, HY) is None


def test_the_edge_of_the_grid_counts_as_unwalked():
    # 0.3 m in from the left edge: the cells beyond it were never walked.
    code, _ = check_home(_floor(), _pgm(_image()), [], 0.325, HY)
    assert code == "home_not_clear"


def test_a_floor_with_no_planning_grid_cannot_check_a_home():
    code, message = check_home(_floor(), None, [], HX, HY)
    assert code == "home_no_planning_grid"
    assert "planning grid" in message


def test_inside_a_no_go_zone_is_refused_by_name():
    code, message = check_home(_floor(), _pgm(_image()), [_zone("no_go", _square(HX, HY, 0.2))], HX, HY)
    assert code == "home_not_clear"
    assert "Plant room" in message


def test_close_to_a_no_go_zone_is_refused_too():
    # The bridge refuses a goal within its zone margin of a no-go edge, so a
    # home there would be a Return home that never starts.
    zone = _zone("no_go", _square(HX + 0.5, HY, 0.2))           # edge 0.3 m away
    assert check_home(_floor(), _pgm(_image()), [zone], HX, HY)[0] == "home_not_clear"
    far = _zone("no_go", _square(HX + 0.9, HY, 0.2))            # edge 0.7 m away
    assert check_home(_floor(), _pgm(_image()), [far], HX, HY) is None


def test_inside_a_stairs_zone_is_refused():
    zone = _zone("stairs", _square(HX, HY, 0.3), label="East stairs")
    code, message = check_home(_floor(), _pgm(_image()), [zone], HX, HY)
    assert code == "home_not_clear" and "East stairs" in message


def test_with_geofences_home_must_be_well_inside_one():
    inside = _zone("geofence", _square(HX, HY, 0.8), label="Ward")
    assert check_home(_floor(), _pgm(_image()), [inside], HX, HY) is None
    elsewhere = _zone("geofence", [(0.0, 0.0), (0.5, 0.0), (0.5, 0.5)], label="Lobby")
    code, message = check_home(_floor(), _pgm(_image()), [elsewhere], HX, HY)
    assert code == "home_not_clear" and "geofence" in message


# --- the routes ------------------------------------------------------------------


def make_home_bundle(directory: Path, map_id=MAP_ID, image=None, planning=True, floors=1) -> Path:
    """A 2 m x 2 m floor whose planning grid is all walked (or `image`)."""
    directory.mkdir(parents=True, exist_ok=True)
    image = image if image is not None else _image()
    docs = []
    for index in range(floors):
        floor_dir = directory / ("floor%d" % index)
        floor_dir.mkdir()
        (floor_dir / "grid.pgm").write_bytes(_pgm(image))
        (floor_dir / "grid.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        if planning:
            (floor_dir / "planning.pgm").write_bytes(_pgm(image))
        free, occupied, unknown = count_cells(bytes(image))
        docs.append({"index": index, "floor_z_m": 3.0 * index, "resolution_m": RES, "origin_xy_m": [0.0, 0.0],
                     "size_cells": [W, H], "free_cells": free, "occupied_cells": occupied,
                     "unknown_cells": unknown})
    (directory / "site_map.json").write_text(json.dumps({
        "site_map": {"map_id": map_id, "site_id": SITE, "name": "Ward", "source": "slam",
                     "extent_min": [0, 0], "extent_max": [2, 2],
                     "created_at": "2026-11-26T00:00:00Z", "updated_at": "2026-11-26T00:00:00Z"},
        "floors": docs,
    }))
    return directory


class Audit:
    def __init__(self):
        self.rows = []

    def __call__(self, action, target, outcome, detail, **kw):
        self.rows.append({"action": action, "target": target, "detail": detail, **kw})


@pytest.fixture
def maps(tmp_path):
    audit = Audit()
    router = build_maps_router(str(tmp_path / "maps"), audit=audit, jobs_dir=str(tmp_path / "jobs"),
                               start_jobs=False)
    router.store.save_bundle(make_home_bundle(tmp_path / "bundle"))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), router.store, audit


HOME = {"x_m": HX, "y_m": HY, "yaw_rad": 1.5}


def test_put_home_stores_it_on_the_floor_with_the_servers_stamp(maps):
    client, store, audit = maps
    response = client.put("/maps/%s/floors/0/home" % MAP_ID,
                          json={**HOME, "set_at": "1999-01-01T00:00:00Z", "set_by": "mallory"})
    assert response.status_code == 200, response.text
    home = response.json()["floors"][0]["home"]
    assert (home["x_m"], home["y_m"], home["yaw_rad"]) == (HX, HY, 1.5)
    assert home["set_at"] != "1999-01-01T00:00:00Z" and home["set_at"].startswith("20")
    assert home["set_by"] != "mallory"
    assert store.get(MAP_ID).floors[0].home.yaw_rad == 1.5
    (row,) = [r for r in audit.rows if r["action"] == "map.home"]
    assert row["target"] == MAP_ID and row["site_id"] == SITE
    assert row["detail"]["floor"] == 0 and row["detail"]["cleared"] is False


def test_setting_home_keeps_an_active_map_active_and_its_content_hash(maps):
    client, store, _ = maps
    store.set_status(MAP_ID, "active", zones_reviewed=True)
    before = store.get(MAP_ID)
    versions = store.current_version(MAP_ID)

    assert client.put("/maps/%s/floors/0/home" % MAP_ID, json=HOME).status_code == 200
    after = store.get(MAP_ID)
    assert after.status == "active"
    assert after.floors[0].content_hash == before.floors[0].content_hash is not None
    assert store.current_version(MAP_ID) == versions

    assert client.delete("/maps/%s/floors/0/home" % MAP_ID).status_code == 200
    cleared = store.get(MAP_ID)
    assert cleared.status == "active" and cleared.floors[0].home is None
    assert cleared.floors[0].content_hash == before.floors[0].content_hash


def test_delete_clears_home_and_is_audited(maps):
    client, store, audit = maps
    client.put("/maps/%s/floors/0/home" % MAP_ID, json=HOME)
    response = client.delete("/maps/%s/floors/0/home" % MAP_ID)
    assert response.status_code == 200
    assert response.json()["floors"][0]["home"] is None
    assert [r["detail"]["cleared"] for r in audit.rows if r["action"] == "map.home"] == [False, True]
    # Removing a home that is not there is not an error: two tabs may both press Remove.
    assert client.delete("/maps/%s/floors/0/home" % MAP_ID).status_code == 200


def test_a_home_without_room_to_turn_is_a_422_and_nothing_is_stored(maps):
    client, store, audit = maps
    response = client.put("/maps/%s/floors/0/home" % MAP_ID, json={**HOME, "x_m": 0.2})
    assert response.status_code == 422
    detail = response.json()["detail"]
    # 0.2 m from the edge of the grid, beyond which nothing was walked.
    assert detail["code"] == "home_not_clear" and "0.45 m" in detail["message"]
    assert store.get(MAP_ID).floors[0].home is None
    assert [r for r in audit.rows if r["action"] == "map.home"] == []


def test_a_floor_with_no_planning_grid_refuses_a_home(tmp_path):
    router = build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"), start_jobs=False)
    router.store.save_bundle(make_home_bundle(tmp_path / "bundle", planning=False))
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).put("/maps/%s/floors/0/home" % MAP_ID, json=HOME)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "home_no_planning_grid"


def test_a_home_inside_a_no_go_zone_is_refused(maps):
    client, store, _ = maps
    store.save_zones(MAP_ID, [_zone("no_go", _square(HX, HY, 0.2))])
    response = client.put("/maps/%s/floors/0/home" % MAP_ID, json=HOME)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "home_not_clear"


@pytest.mark.parametrize("path", ["/maps/map-nope/floors/0/home", "/maps/%s/floors/3/home" % MAP_ID])
def test_an_unknown_map_or_floor_is_a_404(maps, path):
    client, _, _ = maps
    assert client.put(path, json=HOME).status_code == 404
    assert client.delete(path).status_code == 404


@pytest.mark.parametrize("body", [
    {"x_m": HX, "y_m": HY},                                    # a home is a pose: heading required
    {"x_m": "here", "y_m": HY, "yaw_rad": 0.0},
])
def test_a_body_that_is_not_a_pose_is_a_422(maps, body):
    client, store, _ = maps
    assert client.put("/maps/%s/floors/0/home" % MAP_ID, json=body).status_code == 422
    assert store.get(MAP_ID).floors[0].home is None


def test_a_non_finite_pose_is_a_422(maps):
    client, store, _ = maps
    response = client.put("/maps/%s/floors/0/home" % MAP_ID, content='{"x_m": NaN, "y_m": 1.0, "yaw_rad": 0}',
                          headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert store.get(MAP_ID).floors[0].home is None


def test_home_survives_a_planning_edit_and_a_re_survey(maps, tmp_path):
    # Review focus 4: replacing the planning grid keeps the home (the robot's
    # planner decides whether it is still reachable), as zones are kept.
    client, store, _ = maps
    client.put("/maps/%s/floors/0/home" % MAP_ID, json=HOME)
    store.edit_planning(MAP_ID, 0, _pgm(_image([(20, 20, OCCUPIED)])))
    assert store.get(MAP_ID).floors[0].home.x_m == HX
    store.save_bundle(make_home_bundle(tmp_path / "again", floors=2))
    again = store.get(MAP_ID)
    assert again.floors[0].home.x_m == HX and again.floors[1].home is None


def test_the_home_routes_need_what_drawing_zones_needs():
    zones = ROUTES[("PUT", "/maps/{map_id}/zones")]
    assert ROUTES[("PUT", "/maps/{map_id}/floors/{index}/home")] == zones
    assert ROUTES[("DELETE", "/maps/{map_id}/floors/{index}/home")] == zones


# --- RETURN at the command intake ----------------------------------------------


class _Mqtt:
    def __init__(self):
        self.sent = []

    def publish_command(self, robot_id, command):
        self.sent.append(command)


@pytest.fixture
def intake(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_home_bundle(tmp_path / "bundle"))
    store.save_bundle(make_home_bundle(tmp_path / "far", map_id="map-far"))
    registry = RobotRegistry()
    registry.upsert_from_registration("rover-a", SITE, "quad", schema_version=SCHEMA_VERSION)
    registry.upsert_from_registration("rover-old", SITE, "quad", schema_version="0.0.32")
    registry.upsert_from_registration("rover-b", "site-other", "quad", schema_version=SCHEMA_VERSION)
    mqtt = _Mqtt()
    app = FastAPI()
    app.include_router(build_router(CommandStore(), mqtt, registry=registry, map_lookup=store.get))
    return TestClient(app), store, mqtt


def _set_home(store, x=HX, y=HY, yaw=1.5, map_id=MAP_ID):
    from shared.schemas.maps import MapHome
    store.set_home(map_id, 0, MapHome(x_m=x, y_m=y, yaw_rad=yaw, set_at="2026-11-26T00:00:00Z"))


def _return(client, robot="rover-a", **over):
    params = {"x_m": HX, "y_m": HY, "yaw_rad": 1.5, "map_id": MAP_ID, "floor_id": "0", **over}
    return client.post("/robots/%s/commands" % robot,
                       json={"type": "RETURN", "label": "Return home", "initiated_by": "x", "params": params})


def test_return_to_the_stored_home_is_sent_as_it_came(intake):
    client, store, mqtt = intake
    _set_home(store)
    response = _return(client, map_hash="a" * 64)
    assert response.status_code == 200, response.text
    (command,) = mqtt.sent
    assert command.type == "RETURN"
    assert command.params == {"x_m": HX, "y_m": HY, "yaw_rad": 1.5, "map_id": MAP_ID, "floor_id": "0",
                              "map_hash": "a" * 64}


def test_within_a_millimetre_and_a_milliradian_is_the_same_home(intake):
    client, store, mqtt = intake
    _set_home(store, yaw=math.pi)
    assert _return(client, x_m=HX + 0.0009, yaw_rad=-math.pi + 0.0005).status_code == 200
    assert len(mqtt.sent) == 1


@pytest.mark.parametrize("over", [{"x_m": HX + 0.002}, {"y_m": HY - 0.002}, {"yaw_rad": 1.502}])
def test_a_home_that_moved_since_the_console_read_it_is_a_409(intake, over):
    client, store, mqtt = intake
    _set_home(store)
    response = _return(client, **over)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "home_moved" and "reload" in detail["message"]
    assert mqtt.sent == []


def test_a_floor_with_no_home_is_a_409(intake):
    client, _, mqtt = intake
    response = _return(client)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "home_missing"
    assert mqtt.sent == []


@pytest.mark.parametrize("over", [{"map_id": "map-nope"}, {"floor_id": "3"}, {"floor_id": "ground"}])
def test_return_to_an_unknown_map_or_floor_is_a_404(intake, over):
    client, store, mqtt = intake
    _set_home(store)
    assert _return(client, **over).status_code == 404
    assert mqtt.sent == []


def test_a_map_at_another_site_reads_as_not_found(intake):
    client, store, mqtt = intake
    _set_home(store)
    assert _return(client, robot="rover-b").status_code == 404
    assert mqtt.sent == []


@pytest.mark.parametrize("over, field", [
    ({"yaw_rad": None}, "yaw_rad"),
    ({"x_m": float("inf")}, "x_m"),
    ({"map_id": None}, "map_id"),
])
def test_params_that_are_not_a_home_are_a_422(intake, over, field):
    client, store, mqtt = intake
    _set_home(store)
    response = _return(client, **over)
    assert response.status_code == 422
    assert response.json()["detail"] == "RETURN params are not a home to walk to (%s)" % field
    assert mqtt.sent == []


def test_a_bridge_older_than_0_0_33_is_told_to_update(intake):
    client, store, mqtt = intake
    _set_home(store)
    response = _return(client, robot="rover-old")
    assert response.status_code == 409
    assert response.json()["detail"] == ("update the edge device: it runs schema 0.0.32, "
                                         "returning home needs %s" % RETURN_SCHEMA_VERSION)
    assert RETURN_SCHEMA_VERSION == "0.0.33"
    assert mqtt.sent == []


# --- signed in: who may set home, and who it is stamped with ----------------


@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    """The real app with sign-in on (tests/test_site_setup.py's users) and a
    home-able map at their site."""
    pytest.importorskip("numpy")
    import api.app as backend
    from tests import test_site_setup as site_setup

    auth = site_setup.auth_on.__wrapped__(None, tmp_path, monkeypatch)
    store = backend.maps_router.store
    map_id = "map-home-%s" % tmp_path.name[-8:].replace("_", "-").strip("-")
    store.save_bundle(make_home_bundle(tmp_path / "signed-in", map_id=map_id))
    yield TestClient(backend.app), auth, map_id, site_setup
    store.delete(map_id)


@pytest.mark.parametrize("user, allowed", [
    ("sue", True),      # supervisor: editZones
    ("eve", True),      # engineer: every capability since 2026-11-27
    ("ann", False), ("tom", False), ("vic", False),
])
def test_who_may_set_and_remove_home(signed_in, user, allowed):
    client, _, map_id, site_setup = signed_in
    headers = site_setup._bearer(client, user)
    path = "/maps/%s/floors/0/home" % map_id
    assert client.put(path, json=HOME, headers=headers).status_code == (200 if allowed else 403), user
    assert client.delete(path, headers=headers).status_code == (200 if allowed else 403), user


def test_home_is_stamped_with_the_signed_in_user_and_a_device_may_not_set_it(signed_in):
    client, auth, map_id, site_setup = signed_in
    path = "/maps/%s/floors/0/home" % map_id
    sue = auth.users.check_credentials("sue", site_setup.PASSWORD)
    response = client.put(path, json={**HOME, "set_by": "mallory"}, headers=site_setup._bearer(client, "sue"))
    assert response.json()["floors"][0]["home"]["set_by"] == sue.user_id
    assert client.put(path, json=HOME, headers=site_setup.DEVICE_A).status_code == 403
