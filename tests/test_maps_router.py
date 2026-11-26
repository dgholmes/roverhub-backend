"""Map routes (maps/router.py): upload a bundle, list, read, serve, delete."""
from __future__ import annotations

import json
import os
import shutil
import urllib.parse

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api.app as backend
from auth.policy import ROUTES, role_can
from maps.router import build_maps_router
from maps.store import MAX_CLOUD_BYTES
from tests.map_bundles import CLOUD, PGM, PNG, bundle_parts, make_bundle


@pytest.fixture
def router(tmp_path):
    return build_maps_router(str(tmp_path / "maps"))


@pytest.fixture
def client(router):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _files(parts):
    return {name: (name.rsplit("/", 1)[-1], data, "application/octet-stream")
            for name, data in parts.items()}


def _upload(client, tmp_path, **bundle):
    return client.post("/maps", files=_files(bundle_parts(make_bundle(tmp_path / "bundle", **bundle))))


def test_post_maps_accepts_a_bundle_and_returns_the_site_map(client, tmp_path):
    response = _upload(client, tmp_path, cloud=True)
    assert response.status_code == 201
    body = response.json()
    assert body["map_id"] == "map-venue"
    assert body["name"] == "Venue"
    assert body["status"] == "draft"
    assert body["floors"][0]["free_cells"] == 3
    # surveyed_fraction is a property, not a field: it is not on the wire.
    assert "surveyed_fraction" not in body["floors"][0]


def test_get_maps_lists_what_was_uploaded(client, tmp_path):
    assert client.get("/maps").json() == {"items": []}
    _upload(client, tmp_path)
    items = client.get("/maps").json()["items"]
    assert [m["map_id"] for m in items] == ["map-venue"]


def test_get_maps_can_be_filtered_by_site(client, tmp_path):
    _upload(client, tmp_path, map_id="map-a", site_id="site-a")
    client.post("/maps", files=_files(bundle_parts(
        make_bundle(tmp_path / "other", map_id="map-b", site_id="site-b"))))
    assert [m["map_id"] for m in client.get("/maps?site_id=site-b").json()["items"]] == ["map-b"]
    assert len(client.get("/maps").json()["items"]) == 2


def test_get_map_returns_it(client, tmp_path):
    _upload(client, tmp_path, floors=2)
    body = client.get("/maps/map-venue").json()
    assert body["map_id"] == "map-venue"
    assert [f["index"] for f in body["floors"]] == [0, 1]


def test_get_floor_preview_returns_png(client, tmp_path):
    _upload(client, tmp_path)
    response = client.get("/maps/map-venue/floors/0/preview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG


def test_get_floor_grid_and_cloud_are_served_byte_for_byte(client, tmp_path):
    _upload(client, tmp_path, cloud=True)
    grid = client.get("/maps/map-venue/floors/0/grid")
    assert grid.headers["content-type"] == "image/x-portable-graymap"
    assert grid.content == PGM
    cloud = client.get("/maps/map-venue/floors/0/cloud")
    assert cloud.headers["content-type"] == "application/octet-stream"
    assert cloud.content == CLOUD


def test_served_artifacts_are_revalidated_because_a_reupload_replaces_them(client, tmp_path):
    _upload(client, tmp_path)
    assert client.get("/maps/map-venue/floors/0/preview").headers["cache-control"] == "no-cache"


def test_get_cloud_refuses_over_8mb(client, router, tmp_path):
    # The second enforcement point after the packer. The file is put there
    # directly: the upload refuses a cloud this size too (tested below).
    _upload(client, tmp_path, cloud=True)
    cloud = router.store.artifact_path("map-venue", 0, "cloud")
    cloud.write_bytes(b"\x00" * (MAX_CLOUD_BYTES + 12))
    response = client.get("/maps/map-venue/floors/0/cloud")
    assert response.status_code == 413
    # The packer's own limit is still served: this is a limit, not a ban.
    cloud.write_bytes(b"\x00" * MAX_CLOUD_BYTES)
    assert client.get("/maps/map-venue/floors/0/cloud").status_code == 200


def test_upload_refuses_a_cloud_over_8mb_and_stores_nothing(client, router, tmp_path):
    parts = bundle_parts(make_bundle(tmp_path / "bundle", cloud=True))
    parts["floor0/cloud.bin"] = b"\x00" * (MAX_CLOUD_BYTES + 12)
    response = client.post("/maps", files=_files(parts))
    assert response.status_code == 413
    assert router.store.list() == []


def test_unknown_map_returns_404(client):
    assert client.get("/maps/map-nope").status_code == 404
    assert client.get("/maps/map-nope/floors/0/preview").status_code == 404
    assert client.delete("/maps/map-nope").status_code == 404


def test_unknown_floor_kind_or_missing_cloud_returns_404(client, tmp_path):
    _upload(client, tmp_path, cloud=False)
    assert client.get("/maps/map-venue/floors/1/preview").status_code == 404
    assert client.get("/maps/map-venue/floors/-1/preview").status_code == 404
    assert client.get("/maps/map-venue/floors/0/site_map").status_code == 404
    assert client.get("/maps/map-venue/floors/0/cloud").status_code == 404
    assert client.get("/maps/map-venue/floors/zero/preview").status_code == 422


def _plant_copies_outside_the_store(tmp_path):
    """Copies of a real, servable map where a traversal would land: the store's
    parent (reached by the id "..") and a sibling directory (reached by
    "..\\outside", or by its absolute path). If the id guard is removed these are
    read; the router's own path matching has nothing to do with it."""
    stored = tmp_path / "maps" / "map-venue"
    sibling = tmp_path / "outside"
    for target, map_id in ((tmp_path, ".."), (sibling, str(sibling))):
        target.mkdir(exist_ok=True)
        for name in ("floor0",):
            shutil.copytree(stored / name, target / name, dirs_exist_ok=True)
        doc = json.loads((stored / "map.json").read_text())
        doc["map_id"] = map_id
        (target / "map.json").write_text(json.dumps(doc))
    return sibling


def test_an_id_that_climbs_out_of_the_store_reads_nothing(client, tmp_path):
    _upload(client, tmp_path, cloud=True)
    sibling = _plant_copies_outside_the_store(tmp_path)
    assert (tmp_path / "floor0" / "grid.pgm").read_bytes() == PGM  # the bait is real
    absolute = urllib.parse.quote(str(sibling), safe="")
    for map_id in ("%2E%2E", "..%5Coutside", absolute):
        for suffix in ("", "/floors/0/grid", "/floors/0/preview", "/floors/0/cloud"):
            response = client.get("/maps/" + map_id + suffix)
            assert response.status_code == 404, (map_id, suffix, response.status_code)
        assert client.delete("/maps/" + map_id).status_code == 404
    assert (tmp_path / "floor0" / "grid.pgm").exists()  # and DELETE did not reach it
    assert (sibling / "floor0" / "grid.pgm").exists()


@pytest.mark.parametrize("path", [
    "/maps/map-venue/floors/0/..%2Fmap.json",
    "/maps/map-venue/floors/0/..%5Cmap.json",
    "/maps/map-venue/floors/0/grid.pgm",
    "/maps/map-venue/floors/0/map.json",
    "/maps/map-venue/floors/0/%2E%2E",
])
def test_a_kind_that_is_not_one_of_the_three_reads_nothing(client, tmp_path, path):
    _upload(client, tmp_path, cloud=True)
    assert client.get(path).status_code in (404, 422)


def test_reuploading_a_map_replaces_it(client, tmp_path):
    _upload(client, tmp_path, floors=2)
    response = client.post("/maps", files=_files(bundle_parts(
        make_bundle(tmp_path / "again", floors=1))))
    assert response.status_code == 201
    assert len(client.get("/maps/map-venue").json()["floors"]) == 1
    assert client.get("/maps/map-venue/floors/1/grid").status_code == 404
    assert len(client.get("/maps").json()["items"]) == 1


def test_delete_removes_the_map(client, tmp_path):
    _upload(client, tmp_path, cloud=True)
    assert client.delete("/maps/map-venue").status_code == 204
    assert client.get("/maps/map-venue").status_code == 404
    assert client.get("/maps/map-venue/floors/0/grid").status_code == 404
    assert client.get("/maps").json() == {"items": []}


def test_post_without_a_site_map_json_is_a_400(client, tmp_path):
    parts = bundle_parts(make_bundle(tmp_path / "bundle"))
    del parts["site_map.json"]
    assert client.post("/maps", files=_files(parts)).status_code == 400


def test_post_with_an_incomplete_bundle_is_a_400_with_a_reason(client, router, tmp_path):
    parts = bundle_parts(make_bundle(tmp_path / "bundle"))
    del parts["floor0/grid.pgm"]
    response = client.post("/maps", files=_files(parts))
    assert response.status_code == 400
    assert "grid.pgm" in response.json()["detail"]
    assert router.store.list() == []


def test_post_with_nothing_is_a_400(client):
    assert client.post("/maps").status_code == 400
    assert client.post("/maps", data={"x": "y"}).status_code == 400


@pytest.mark.parametrize("name", [
    "../evil.bin", "floor0/../../evil", "/etc/passwd", "floor0/notes.txt", "notes.txt",
    "floor0/grid.pgm/", "floor-1/grid.pgm", "floor0\\grid.pgm", "C:/x",
    "floor\u0661/grid.pgm",  # an Arabic-Indic digit is not one of 0-9
])
def test_post_refuses_a_part_the_layout_does_not_name(client, router, tmp_path, name):
    parts = bundle_parts(make_bundle(tmp_path / "bundle"))
    parts[name] = b"x"
    assert client.post("/maps", files=_files(parts)).status_code == 400
    assert router.store.list() == []
    assert not (tmp_path / "evil.bin").exists()


def test_post_refuses_a_bundle_that_names_itself_outside_the_store(client, router, tmp_path):
    parts = bundle_parts(make_bundle(tmp_path / "bundle", map_id="../escape"))
    assert client.post("/maps", files=_files(parts)).status_code == 400
    assert not (tmp_path / "escape").exists()


def test_post_over_the_request_cap_is_refused_before_it_is_read(tmp_path):
    small = build_maps_router(str(tmp_path / "maps"), max_upload_bytes=100)
    app = FastAPI()
    app.include_router(small)
    client = TestClient(app)
    response = client.post("/maps", files=_files(bundle_parts(make_bundle(tmp_path / "bundle"))))
    assert response.status_code == 413
    assert small.store.list() == []


def test_a_chunked_upload_with_no_content_length_is_cut_off_at_the_cap(tmp_path):
    # No Content-Length at all, which is what the declared-length check cannot
    # see. The body is counted as it is read and refused once it passes the cap.
    small = build_maps_router(str(tmp_path / "maps"), max_upload_bytes=1000)
    app = FastAPI()
    app.include_router(small)
    client = TestClient(app)

    def body():
        yield b'--x\r\nContent-Disposition: form-data; name="site_map.json"; filename="a"\r\n\r\n'
        for _ in range(40):
            yield b"y" * 500

    request = client.build_request("POST", "/maps", content=body(),
                                   headers={"content-type": "multipart/form-data; boundary=x"})
    assert "content-length" not in request.headers  # httpx sends an iterator chunked
    assert request.headers["transfer-encoding"] == "chunked"
    response = client.send(request)
    assert response.status_code == 413
    assert small.store.list() == []
    assert os.listdir(tmp_path / "maps") == []  # no spooled leftovers in the store


def test_a_part_that_is_not_a_file_is_a_400(client, tmp_path):
    parts = bundle_parts(make_bundle(tmp_path / "bundle"))
    response = client.post("/maps", files=_files(parts), data={"floor0/grid.pgm": "text"})
    assert response.status_code == 400


# --- access rules -----------------------------------------------------------


def test_every_maps_route_has_an_auth_policy_rule(router):
    routes = [(m, r.path) for r in router.routes if isinstance(r, APIRoute) for m in r.methods]
    # 18, plus the map editor's PUT .../moved and POST .../cloud-edits (2026-11-16),
    # plus the map ready checklist's POST .../trim and PATCH /maps/{id} (0.0.29),
    # plus a floor's home, PUT and DELETE .../home (0.0.33).
    assert len(routes) == 24
    for key in routes:
        assert key in ROUTES, key


def test_the_maps_routes_are_registered_on_the_app():
    app_routes = {(m, r.path) for r in backend.app.routes if isinstance(r, APIRoute) for m in r.methods}
    for key in [("GET", "/maps"), ("GET", "/maps/{map_id}"), ("POST", "/maps"),
                ("GET", "/maps/{map_id}/floors/{index}/{kind}"), ("DELETE", "/maps/{map_id}"),
                ("PUT", "/maps/{map_id}/zones"), ("PUT", "/maps/{map_id}/status")]:
        assert key in app_routes, key


def test_everyone_may_read_a_map_but_only_configuration_may_change_one():
    for key in [("GET", "/maps"), ("GET", "/maps/{map_id}"),
                ("GET", "/maps/{map_id}/floors/{index}/{kind}")]:
        assert any(role_can("viewer", c) for c in ROUTES[key].capabilities), key
    for key in [("POST", "/maps"), ("DELETE", "/maps/{map_id}")]:
        capabilities = ROUTES[key].capabilities
        assert capabilities == ("configure",), key
        assert not any(role_can("viewer", c) for c in capabilities)
        assert not any(role_can("operator", c) for c in capabilities)
        assert any(role_can("engineer", c) for c in capabilities)


# ==== delete refuses a map that is in use =======================================


def _client_with(tmp_path, in_use):
    app = FastAPI()
    app.include_router(build_maps_router(str(tmp_path / "maps"), in_use=in_use))
    return TestClient(app)


@pytest.mark.parametrize("reason", [
    "mission Pictlab is drawn on it (move or delete it first)",
    "task Nightly (Pictlab) runs a mission on it (delete it first)",
    "robot rover-jetson runs it (switch it to another map first)",
])
def test_delete_is_refused_with_409_naming_what_is_in_the_way(tmp_path, reason):
    seen = []
    client = _client_with(tmp_path, lambda m: seen.append(m.map_id) or [reason])
    _upload(client, tmp_path, cloud=True)
    response = client.delete("/maps/map-venue")
    assert response.status_code == 409
    # One string, map named by its name, every reason in it: the console shows it verbatim.
    assert response.json()["detail"] == "map Venue is in use: " + reason
    assert seen == ["map-venue"]
    assert client.get("/maps/map-venue").status_code == 200  # nothing was deleted


def test_delete_409_joins_every_reason(tmp_path):
    client = _client_with(tmp_path, lambda m: ["mission A is drawn on it (move or delete it first)",
                                               "robot rover-pi runs it (switch it to another map first)"])
    _upload(client, tmp_path)
    detail = client.delete("/maps/map-venue").json()["detail"]
    assert detail == ("map Venue is in use: mission A is drawn on it (move or delete it first); "
                      "robot rover-pi runs it (switch it to another map first)")


def test_delete_goes_ahead_when_nothing_uses_the_map(tmp_path):
    client = _client_with(tmp_path, lambda m: [])
    _upload(client, tmp_path)
    assert client.delete("/maps/map-venue").status_code == 204
    assert client.get("/maps/map-venue").status_code == 404


def test_delete_of_an_unknown_map_is_404_and_never_asks_what_uses_it(tmp_path):
    asked = []
    client = _client_with(tmp_path, lambda m: asked.append(m) or ["x"])
    assert client.delete("/maps/map-nope").status_code == 404
    assert asked == []


# ---- the app's own rule (api/app.py _map_in_use) ------------------------------


@pytest.fixture
def uses(tmp_path, monkeypatch):
    """The app's mission and task stores and registry, empty, in tmp_path."""
    from missions.router import MissionStore
    from tasks.store import TaskStore
    missions = MissionStore(str(tmp_path / "missions"))
    tasks = TaskStore(str(tmp_path / "tasks"))
    monkeypatch.setattr(backend, "_scheduler_missions", missions)
    monkeypatch.setattr(backend, "_reconcile_task_store", tasks)
    backend.registry._robots.clear()
    yield missions, tasks
    backend.registry._robots.clear()


def _mission(mission_id, name, map_id):
    from shared.schemas.missions import MissionLibraryEntry
    return MissionLibraryEntry(mission_id=mission_id, name=name, type="patrol", kind="program",
                               creator="x", updated_at="2026-11-26T00:00:00Z", map_id=map_id,
                               program_state="{}", site_id="site-venue")


def _task(task_id, mission_id, name=None):
    from shared.schemas.tasks import MissionTask
    now = "2026-11-26T00:00:00Z"
    return MissionTask(task_id=task_id, mission_id=mission_id, site_id="site-venue", name=name,
                       created_by="x", created_at=now, updated_at=now)


class _Map:
    map_id = "map-office"
    name = "Office"
    site_id = "site-venue"


def test_map_in_use_is_empty_for_a_free_map(uses):
    missions, _ = uses
    missions.put(_mission("m-elsewhere", "Lobby", "map-lobby"))
    backend.registry.upsert_from_registration("rover-pi", "site-venue", "quad", nav_map_id="map-lobby")
    assert backend._map_in_use(_Map()) == []


def test_map_in_use_names_a_mission_drawn_on_it(uses):
    missions, _ = uses
    missions.put(_mission("m-1", "Pictlab", "map-office"))
    assert backend._map_in_use(_Map()) == ["mission Pictlab is drawn on it (move or delete it first)"]


def test_map_in_use_names_a_task_whose_mission_is_on_it(uses):
    missions, tasks = uses
    missions.put(_mission("m-1", "Pictlab", "map-office"))
    tasks.put_task(_task("task-1", "m-1", name="Nightly"))
    tasks.put_task(_task("task-2", "m-other"))  # a mission not on this map: not named
    assert backend._map_in_use(_Map())[1] == "task Nightly (Pictlab) runs a mission on it (delete it first)"


def test_map_in_use_names_a_robot_that_runs_it(uses):
    backend.registry.upsert_from_registration("rover-jetson", "site-venue", "quad", nav_map_id="map-office")
    backend.registry.upsert_from_registration("rover-pi", "site-venue", "quad")
    assert backend._map_in_use(_Map()) == ["robot rover-jetson runs it (switch it to another map first)"]


def test_the_app_refuses_to_delete_a_map_in_use_with_one_sentence(uses, monkeypatch):
    missions, _ = uses
    missions.put(_mission("m-1", "Pictlab", "map-office"))
    backend.registry.upsert_from_registration("rover-jetson", "site-venue", "quad", nav_map_id="map-office")
    deleted = []
    monkeypatch.setattr(backend.maps_router.store, "get", lambda map_id: _Map() if map_id == "map-office" else None)
    monkeypatch.setattr(backend.maps_router.store, "delete", lambda map_id: deleted.append(map_id) or True)
    response = TestClient(backend.app).delete("/maps/map-office")
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "map Office is in use: mission Pictlab is drawn on it (move or delete it first); "
        "robot rover-jetson runs it (switch it to another map first)")
    assert deleted == []
