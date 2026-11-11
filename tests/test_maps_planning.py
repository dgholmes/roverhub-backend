"""A floor's planning grid: the map Nav2 plans on, kept beside the survey grid.

maps/store.py (set_planning, delete_planning, bundles that carry one),
maps/router.py (GET/PUT/DELETE /maps/{map_id}/floors/{index}/planning), its
rules in auth/policy.py, and the maps.set_planning command line.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api.app as backend
import maps.router as maps_router_module
from auth.policy import ROUTES, role_can
from auth.tokens import TokenIssuer
from auth.users import UserStore
from maps import set_planning as cli
from maps.router import build_maps_router
from maps.store import (
    FREE_THRESH, OCCUPIED_THRESH, BundleError, FloorNotFound, MapNotFound, MapStore,
    PlanningNotFound, count_cells,
)
from shared.schemas.maps import MapZone
from tests.map_bundles import bundle_parts, make_bundle

# The bundle's floors are 4 x 2. Each value sits on one side of a threshold:
# 0, 89 occupied ((255-89)/255 = 0.651 > 0.65); 90 and 205 unknown (0.647 is not
# > 0.65, and 50/255 = 0.19608 is not < 0.196); 206, 254, 255 free.
PIXELS = bytes([0, 89, 90, 205, 205, 206, 254, 255])
COUNTS = {"free_cells": 3, "occupied_cells": 2, "unknown_cells": 3}
PLANNING = b"P5\n4 2\n255\n" + PIXELS
# Nav2's map_saver writes a comment after the magic number.
PLANNING_WITH_COMMENT = b"P5\n# CREATOR: map_saver.cpp 0.050 m/pix\n4 2\n255\n" + PIXELS
OTHER = b"P5\n4 2\n255\n" + bytes([254] * 8)


def _counts(planning):
    return {k: getattr(planning, k) for k in COUNTS}


@pytest.fixture
def store(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b"))
    return store


def _floor_dir(tmp_path, map_id="map-venue", index=0):
    return tmp_path / "maps" / map_id / ("floor%d" % index)


# --- counting ---------------------------------------------------------------


def test_counts_follow_nav2s_thresholds_for_every_byte():
    assert (OCCUPIED_THRESH, FREE_THRESH) == (0.65, 0.196)
    free, occupied, unknown = count_cells(bytes(range(256)))
    expected_occupied = sum(1 for v in range(256) if (255 - v) / 255 > 0.65)
    expected_free = sum(1 for v in range(256) if (255 - v) / 255 < 0.196)
    assert (free, occupied) == (expected_free, expected_occupied)
    assert free + occupied + unknown == 256
    # The three values the map tools write.
    assert count_cells(bytes([0])) == (0, 1, 0)
    assert count_cells(bytes([254])) == (1, 0, 0)
    assert count_cells(bytes([205])) == (0, 0, 1)


# --- store ------------------------------------------------------------------


def test_set_planning_round_trips(store, tmp_path):
    saved = store.set_planning("map-venue", 0, PLANNING)
    for site_map in (saved, store.get("map-venue")):
        planning = site_map.floors[0].planning
        assert _counts(planning) == COUNTS
        assert planning.clearance_m == 0.2
    path = store.artifact_path("map-venue", 0, "planning")
    assert path == _floor_dir(tmp_path) / "planning.pgm"
    assert path.read_bytes() == PLANNING


def test_the_counts_match_a_hand_built_pgm_with_a_header_comment(store):
    planning = store.set_planning("map-venue", 0, PLANNING_WITH_COMMENT, clearance_m=0.35).floors[0].planning
    assert _counts(planning) == COUNTS
    assert planning.clearance_m == 0.35


def test_setting_it_again_replaces_it(store, tmp_path):
    store.set_planning("map-venue", 0, PLANNING)
    planning = store.set_planning("map-venue", 0, OTHER).floors[0].planning
    assert _counts(planning) == {"free_cells": 8, "occupied_cells": 0, "unknown_cells": 0}
    assert (_floor_dir(tmp_path) / "planning.pgm").read_bytes() == OTHER
    assert sorted(os.listdir(_floor_dir(tmp_path))) == ["grid.pgm", "grid.png", "planning.pgm"]


@pytest.mark.parametrize("pgm, reason", [
    (b"P5\n5 2\n255\n" + bytes(10), "5 x 2"),                # wider than the floor
    (b"P5\n2 4\n255\n" + PIXELS, "2 x 4"),                   # same cells, transposed
    (b"P5\n4 2\n255\n" + PIXELS[:-1], "8 cells"),            # short
    (b"P5\n4 2\n255\n" + PIXELS + b"\x00", "8 cells"),       # long
    (b"P2\n4 2\n255\n0 0 0 0 0 0 0 0\n", "P5"),              # ASCII PGM
    (b"\x89PNG\r\n\x1a\n", "P5"),
    (b"", "P5"),
    (b"P5\n4 2\n65535\n" + bytes(16), "maxval"),
])
def test_a_grid_that_is_not_the_floors_size_or_not_a_pgm_is_refused(store, tmp_path, pgm, reason):
    with pytest.raises(BundleError, match=reason):
        store.set_planning("map-venue", 0, pgm)
    assert store.get("map-venue").floors[0].planning is None
    assert sorted(os.listdir(_floor_dir(tmp_path))) == ["grid.pgm", "grid.png"]


def test_the_wrong_size_is_refused_with_a_plain_reason(store):
    with pytest.raises(BundleError) as caught:
        store.set_planning("map-venue", 0, b"P5\n5 2\n255\n" + bytes(10))
    assert str(caught.value) == ("floor 0: the planning grid is 5 x 2 cells but the floor's grid is 4 x 2; "
                                 "it must be made in exactly the floor's frame, origin, size and resolution")


@pytest.mark.parametrize("clearance", [0, -0.1, 2, 5, float("nan"), float("inf")])
def test_a_clearance_that_is_not_a_distance_is_refused(store, clearance):
    with pytest.raises(BundleError, match="clearance_m"):
        store.set_planning("map-venue", 0, PLANNING, clearance_m=clearance)
    assert store.get("map-venue").floors[0].planning is None


def test_an_unknown_map_or_floor_is_not_found(store):
    with pytest.raises(MapNotFound):
        store.set_planning("map-nope", 0, PLANNING)
    with pytest.raises(MapNotFound):
        store.set_planning("../escape", 0, PLANNING)
    for floor in (1, -1, True, "0"):
        with pytest.raises(FloorNotFound):
            store.set_planning("map-venue", floor, PLANNING)
    with pytest.raises(FloorNotFound):
        store.delete_planning("map-venue", 3)


def test_setting_and_removing_it_leave_the_status_and_zones_alone(store):
    zone = MapZone(zone_id="z1", map_id="map-venue", kind="no_go", label="Stairwell",
                   polygon=[(0, 0), (1, 0), (1, 1)])
    store.save_zones("map-venue", [zone])
    store.set_status("map-venue", "active")
    for site_map in (store.set_planning("map-venue", 0, PLANNING), store.get("map-venue")):
        assert site_map.status == "active"
        assert [z.zone_id for z in site_map.zones] == ["z1"]
    assert store.delete_planning("map-venue", 0).status == "active"
    assert store.get("map-venue").status == "active"


def test_setting_it_leaves_the_rest_of_the_map_alone(store):
    before = store.get("map-venue")
    after = store.set_planning("map-venue", 0, PLANNING)
    floors = [f.model_copy(update={"planning": None}) for f in after.floors]
    assert after.model_copy(update={"floors": floors}) == before


def test_delete_planning_removes_the_file_and_the_record(store, tmp_path):
    store.set_planning("map-venue", 0, PLANNING)
    removed = store.delete_planning("map-venue", 0)
    assert removed.floors[0].planning is None
    assert store.get("map-venue").floors[0].planning is None
    assert store.artifact_path("map-venue", 0, "planning") is None
    assert sorted(os.listdir(_floor_dir(tmp_path))) == ["grid.pgm", "grid.png"]
    with pytest.raises(PlanningNotFound):
        store.delete_planning("map-venue", 0)


def test_a_failed_map_write_puts_the_old_grid_back(store, tmp_path, monkeypatch):
    store.set_planning("map-venue", 0, PLANNING)

    def failing(directory, site_map):
        raise OSError("disk full")

    monkeypatch.setattr(MapStore, "_write_map_file", staticmethod(failing))
    with pytest.raises(OSError):
        store.set_planning("map-venue", 0, OTHER)
    with pytest.raises(OSError):
        store.delete_planning("map-venue", 0)
    monkeypatch.undo()
    assert _counts(store.get("map-venue").floors[0].planning) == COUNTS
    assert (_floor_dir(tmp_path) / "planning.pgm").read_bytes() == PLANNING
    assert sorted(os.listdir(_floor_dir(tmp_path))) == ["grid.pgm", "grid.png", "planning.pgm"]


def test_a_failed_first_set_leaves_no_file(store, tmp_path, monkeypatch):
    def failing(directory, site_map):
        raise OSError("disk full")

    monkeypatch.setattr(MapStore, "_write_map_file", staticmethod(failing))
    with pytest.raises(OSError):
        store.set_planning("map-venue", 0, PLANNING)
    monkeypatch.undo()
    assert store.get("map-venue").floors[0].planning is None
    assert sorted(os.listdir(_floor_dir(tmp_path))) == ["grid.pgm", "grid.png"]


def test_an_oversized_grid_is_refused(store, monkeypatch):
    monkeypatch.setattr("maps.store.MAX_PLANNING_BYTES", len(PLANNING) - 1)
    with pytest.raises(BundleError, match="exceeds"):
        store.set_planning("map-venue", 0, PLANNING)


# --- bundles ----------------------------------------------------------------


def test_an_old_bundle_without_a_planning_grid_still_loads(store):
    assert [f.planning for f in store.get("map-venue").floors] == [None]


def test_a_map_json_written_before_0_0_17_still_loads(store, tmp_path):
    path = tmp_path / "maps" / "map-venue" / "map.json"
    doc = json.loads(path.read_text())
    for floor in doc["floors"]:
        floor.pop("planning", None)
    path.write_text(json.dumps(doc))
    site_map = store.get("map-venue")
    assert site_map is not None and site_map.floors[0].planning is None
    assert _counts(store.set_planning("map-venue", 0, PLANNING).floors[0].planning) == COUNTS


def test_a_bundle_may_carry_a_planning_grid(tmp_path):
    bundle = make_bundle(tmp_path / "b", floors=2)
    (bundle / "floor1" / "planning.pgm").write_bytes(PLANNING)
    store = MapStore(tmp_path / "maps")
    saved = store.save_bundle(bundle)
    assert saved.floors[0].planning is None
    assert _counts(saved.floors[1].planning) == COUNTS
    assert store.artifact_path("map-venue", 1, "planning").read_bytes() == PLANNING


def test_a_bundles_planning_counts_come_from_the_file_and_its_clearance_from_the_manifest(tmp_path):
    bundle = make_bundle(tmp_path / "b")
    (bundle / "floor0" / "planning.pgm").write_bytes(PLANNING)
    doc = json.loads((bundle / "site_map.json").read_text())
    doc["floors"][0]["planning"] = {"free_cells": 8, "occupied_cells": 0, "unknown_cells": 0,
                                    "clearance_m": 0.3}
    (bundle / "site_map.json").write_text(json.dumps(doc))
    planning = MapStore(tmp_path / "maps").save_bundle(bundle).floors[0].planning
    assert _counts(planning) == COUNTS
    assert planning.clearance_m == 0.3


def test_a_bundle_that_describes_a_planning_grid_it_does_not_carry_is_refused(tmp_path):
    bundle = make_bundle(tmp_path / "b")
    doc = json.loads((bundle / "site_map.json").read_text())
    doc["floors"][0]["planning"] = dict(COUNTS)
    (bundle / "site_map.json").write_text(json.dumps(doc))
    store = MapStore(tmp_path / "maps")
    with pytest.raises(BundleError, match="planning.pgm"):
        store.save_bundle(bundle)
    assert store.list() == []


def test_a_bundle_whose_planning_grid_is_the_wrong_size_is_refused(tmp_path):
    bundle = make_bundle(tmp_path / "b")
    (bundle / "floor0" / "planning.pgm").write_bytes(b"P5\n5 2\n255\n" + bytes(10))
    store = MapStore(tmp_path / "maps")
    with pytest.raises(BundleError, match="5 x 2"):
        store.save_bundle(bundle)
    assert store.list() == []


def test_a_re_upload_does_not_keep_the_old_surveys_planning_grid(store, tmp_path):
    store.set_planning("map-venue", 0, PLANNING)
    saved = store.save_bundle(make_bundle(tmp_path / "again"))
    assert saved.floors[0].planning is None
    assert store.artifact_path("map-venue", 0, "planning") is None


def test_a_store_opened_without_the_sweep_leaves_a_running_servers_scratch_alone(tmp_path):
    root = tmp_path / "maps"
    MapStore(root)
    for name in (".upload-x", ".incoming-x", ".deleted-x"):
        (root / name).mkdir()
    MapStore(root, sweep=False)
    assert sorted(os.listdir(root)) == [".deleted-x", ".incoming-x", ".upload-x"]
    MapStore(root)
    assert os.listdir(root) == []


# --- router -----------------------------------------------------------------


@pytest.fixture
def router(tmp_path):
    return build_maps_router(str(tmp_path / "maps"))


@pytest.fixture
def client(router, tmp_path):
    router.store.save_bundle(make_bundle(tmp_path / "b"))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


URL = "/maps/map-venue/floors/0/planning"


def test_put_get_delete_planning(client, router):
    assert client.get(URL).status_code == 404
    response = client.put(URL, content=PLANNING, headers={"content-type": "image/x-portable-graymap"})
    assert response.status_code == 200
    assert response.json()["floors"][0]["planning"] == {**COUNTS, "clearance_m": 0.2}
    assert client.get("/maps/map-venue").json()["floors"][0]["planning"]["free_cells"] == 3

    served = client.get(URL)
    assert served.status_code == 200
    assert served.content == PLANNING
    assert served.headers["content-type"] == "image/x-portable-graymap"
    assert served.headers["cache-control"] == "no-cache"
    assert "etag" in served.headers

    response = client.delete(URL)
    assert response.status_code == 200
    assert response.json()["floors"][0]["planning"] is None
    assert client.get(URL).status_code == 404
    assert client.delete(URL).status_code == 404


def test_put_planning_takes_a_clearance(client):
    response = client.put(URL + "?clearance_m=0.25", content=PLANNING)
    assert response.json()["floors"][0]["planning"]["clearance_m"] == 0.25
    assert client.put(URL + "?clearance_m=3", content=PLANNING).status_code == 400
    assert client.put(URL + "?clearance_m=wide", content=PLANNING).status_code == 422


def test_put_planning_refuses_the_wrong_size_with_a_reason(client, router):
    response = client.put(URL, content=b"P5\n5 2\n255\n" + bytes(10))
    assert response.status_code == 400
    assert "5 x 2" in response.json()["detail"]
    assert client.put(URL, content=b"not a pgm").status_code == 400
    assert router.store.get("map-venue").floors[0].planning is None


def test_put_planning_on_an_unknown_map_or_floor_is_404(client):
    assert client.put("/maps/map-nope/floors/0/planning", content=PLANNING).status_code == 404
    response = client.put("/maps/map-venue/floors/2/planning", content=PLANNING)
    assert response.status_code == 404
    assert "floor 2" in response.json()["detail"]
    assert client.put("/maps/map-venue/floors/zero/planning", content=PLANNING).status_code == 422
    assert client.delete("/maps/map-nope/floors/0/planning").status_code == 404


def test_put_planning_over_the_cap_is_413(client, monkeypatch):
    monkeypatch.setattr(maps_router_module, "MAX_PLANNING_BYTES", len(PLANNING) - 1)
    assert client.put(URL, content=PLANNING).status_code == 413


def test_put_planning_leaves_the_status_alone(client, router):
    router.store.set_status("map-venue", "active")
    assert client.put(URL, content=PLANNING).json()["status"] == "active"
    assert client.delete(URL).json()["status"] == "active"


def test_a_bundle_upload_may_include_a_planning_grid(client, tmp_path):
    parts = bundle_parts(make_bundle(tmp_path / "up", map_id="map-up"))
    parts["floor0/planning.pgm"] = PLANNING
    files = {name: (name.rsplit("/", 1)[-1], data, "application/octet-stream") for name, data in parts.items()}
    response = client.post("/maps", files=files)
    assert response.status_code == 201
    assert response.json()["floors"][0]["planning"] == {**COUNTS, "clearance_m": 0.2}
    assert client.get("/maps/map-up/floors/0/planning").content == PLANNING


# --- access rules -----------------------------------------------------------


PLANNING_ROUTE = "/maps/{map_id}/floors/{index}/planning"


def test_the_planning_routes_have_rules_and_are_on_the_app(router):
    routes = {(m, r.path) for r in router.routes if isinstance(r, APIRoute) for m in r.methods}
    app_routes = {(m, r.path) for r in backend.app.routes if isinstance(r, APIRoute) for m in r.methods}
    for method in ("GET", "PUT", "DELETE"):
        assert (method, PLANNING_ROUTE) in routes
        assert (method, PLANNING_ROUTE) in app_routes
        assert (method, PLANNING_ROUTE) in ROUTES
    assert ("HEAD", PLANNING_ROUTE) in ROUTES


def test_reading_is_like_the_grid_and_writing_like_zones():
    assert ROUTES[("GET", PLANNING_ROUTE)] == ROUTES[("GET", "/maps/{map_id}/floors/{index}/{kind}")]
    for method in ("PUT", "DELETE"):
        assert ROUTES[(method, PLANNING_ROUTE)] == ROUTES[("PUT", "/maps/{map_id}/zones")]
        capabilities = ROUTES[(method, PLANNING_ROUTE)].capabilities
        assert any(role_can("supervisor", c) for c in capabilities)
        for role in ("viewer", "operator", "technician", "engineer"):
            assert not any(role_can(role, c) for c in capabilities), (method, role)


PASSWORD = "correct horse"


@pytest.fixture
def app_map(tmp_path):
    """A map in the real app's store (MAPS_DIR is a throwaway, see conftest.py)."""
    map_id = "map-planning-" + uuid.uuid4().hex[:8]
    backend.maps_router.store.save_bundle(make_bundle(tmp_path / "app-bundle", map_id=map_id))
    yield map_id
    backend.maps_router.store.delete(map_id)


@pytest.fixture
def auth_on(tmp_path, monkeypatch):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    for username, role in (("vic", "viewer"), ("ana", "operator"), ("eve", "engineer"), ("sue", "supervisor")):
        auth.users.create(username=username, display_name=username, password=PASSWORD,
                          org_role="member", site_roles={"site-venue": role})
    return auth


def _bearer(client, username):
    token = client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_with_sign_in_off_anyone_may_set_and_remove_it(app_map):
    client = TestClient(backend.app)
    url = "/maps/%s/floors/0/planning" % app_map
    assert client.put(url, content=PLANNING).status_code == 200
    assert client.get(url).content == PLANNING
    assert client.delete(url).status_code == 200


def test_with_sign_in_on_everyone_reads_it_and_only_a_supervisor_changes_it(auth_on, app_map):
    client = TestClient(backend.app)
    url = "/maps/%s/floors/0/planning" % app_map
    vic, ana, eve, sue = (_bearer(client, n) for n in ("vic", "ana", "eve", "sue"))
    # Signing in sets a session cookie (good for reads); the signed-out checks
    # use a client that never signed in.
    anonymous = TestClient(backend.app)

    assert anonymous.put(url, content=PLANNING).status_code == 401
    for who in (vic, ana, eve):
        assert client.put(url, content=PLANNING, headers=who).status_code == 403
    assert client.put(url, content=PLANNING, headers=sue).status_code == 200

    assert anonymous.get(url).status_code == 401
    for who in (vic, ana, eve, sue):
        assert client.get(url, headers=who).content == PLANNING

    for who in (vic, ana, eve):
        assert client.delete(url, headers=who).status_code == 403
    assert anonymous.delete(url).status_code == 401
    assert client.delete(url, headers=sue).status_code == 200
    assert client.get(url, headers=vic).status_code == 404


# --- the command line -------------------------------------------------------


def test_the_cli_sets_it_and_prints_the_counts(store, tmp_path, capsys):
    pgm = tmp_path / "office.pgm"
    pgm.write_bytes(PLANNING)
    store.set_status("map-venue", "active")
    assert cli.main([str(tmp_path / "maps"), "map-venue", "0", str(pgm), "--clearance", "0.3"]) == 0
    out = capsys.readouterr().out
    assert "map-venue floor 0: planning grid set (4 x 2 cells, clearance 0.3 m)" in out
    assert "free              3   37.5%" in out
    assert "occupied          2   25.0%" in out
    assert "unknown           3   37.5%" in out
    assert "map status: active (unchanged)" in out
    planning = store.get("map-venue").floors[0].planning
    assert _counts(planning) == COUNTS and planning.clearance_m == 0.3
    assert store.get("map-venue").status == "active"
    assert store.artifact_path("map-venue", 0, "planning").read_bytes() == PLANNING


@pytest.mark.parametrize("args, reason", [
    (["map-venue", "0", "wrong.pgm"], "5 x 2"),
    (["map-nope", "0", "office.pgm"], "no map map-nope"),
    (["map-venue", "4", "office.pgm"], "no floor 4"),
    (["map-venue", "0", "missing.pgm"], "cannot read"),
    (["map-venue", "0", "office.pgm", "--clearance", "0"], "clearance_m"),
])
def test_the_cli_refuses_what_the_store_refuses(store, tmp_path, capsys, args, reason):
    (tmp_path / "office.pgm").write_bytes(PLANNING)
    (tmp_path / "wrong.pgm").write_bytes(b"P5\n5 2\n255\n" + bytes(10))
    args = [args[0], args[1], str(tmp_path / args[2])] + args[3:]
    assert cli.main([str(tmp_path / "maps")] + args) == 2
    assert reason in capsys.readouterr().err
    assert store.get("map-venue").floors[0].planning is None


def test_the_cli_refuses_a_store_that_is_not_there(tmp_path, capsys):
    pgm = tmp_path / "office.pgm"
    pgm.write_bytes(PLANNING)
    assert cli.main([str(tmp_path / "no-such-dir"), "map-venue", "0", str(pgm)]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert not (tmp_path / "no-such-dir").exists()


def test_the_cli_leaves_a_running_servers_upload_alone(store, tmp_path):
    pgm = tmp_path / "office.pgm"
    pgm.write_bytes(PLANNING)
    in_progress = tmp_path / "maps" / ".upload-live"
    in_progress.mkdir()
    assert cli.main([str(tmp_path / "maps"), "map-venue", "0", str(pgm)]) == 0
    assert in_progress.is_dir()
