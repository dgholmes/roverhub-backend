"""Curating a map: zones, freezing it, and what a re-upload does to both."""
from __future__ import annotations

import os
import threading

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from auth.policy import ROUTES, role_can
from maps.router import MAX_ZONES_BODY_BYTES, build_maps_router
from maps.store import MAX_VERTICES_PER_ZONE, MAX_ZONES, MapNotFound, MapStore
from shared.schemas.maps import MapZone
from tests.map_bundles import make_bundle


def zone(zone_id="z1", map_id="map-venue", kind="no_go", label="Stairwell",
         polygon=((0, 0), (1, 0), (1, 1))):
    return MapZone(zone_id=zone_id, map_id=map_id, kind=kind, label=label, polygon=list(polygon))


@pytest.fixture
def store(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b"))
    return store


# --- store: zones -----------------------------------------------------------


def test_save_zones_round_trips(store):
    saved = store.save_zones("map-venue", [zone()])
    assert saved.zones[0].label == "Stairwell"
    loaded = store.get("map-venue")
    assert loaded.zones[0].label == "Stairwell"
    assert loaded.zones[0].polygon == [(0, 0), (1, 0), (1, 1)]


def test_save_zones_replaces_the_whole_list(store):
    store.save_zones("map-venue", [zone("z1"), zone("z2", kind="geofence")])
    store.save_zones("map-venue", [zone("z2", kind="geofence")])
    assert [z.zone_id for z in store.get("map-venue").zones] == ["z2"]
    store.save_zones("map-venue", [])
    assert store.get("map-venue").zones == []


def test_save_zones_leaves_the_rest_of_the_map_alone(store, tmp_path):
    before = store.get("map-venue")
    store.save_zones("map-venue", [zone()])
    after = store.get("map-venue")
    assert after.model_copy(update={"zones": []}) == before
    assert store.artifact_path("map-venue", 0, "grid") is not None
    assert sorted(os.listdir(tmp_path / "maps")) == ["map-venue"]
    assert sorted(os.listdir(tmp_path / "maps" / "map-venue")) == ["floor0", "map.json"]


def test_save_zones_on_an_unknown_map_raises_map_not_found(store):
    with pytest.raises(MapNotFound):
        store.save_zones("map-nope", [zone(map_id="map-nope")])
    with pytest.raises(MapNotFound):
        store.save_zones("../escape", [])


def test_a_zone_for_another_map_is_refused(store):
    with pytest.raises(ValueError, match="map-other"):
        store.save_zones("map-venue", [zone(map_id="map-other")])
    assert store.get("map-venue").zones == []


def test_two_zones_with_one_id_are_refused(store):
    with pytest.raises(ValueError, match="z1"):
        store.save_zones("map-venue", [zone("z1"), zone("z1", label="Other")])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_a_zone_with_a_non_finite_vertex_is_refused(store, bad):
    with pytest.raises(ValueError, match="finite"):
        store.save_zones("map-venue", [zone(polygon=[(0, 0), (1, 0), (bad, 1)])])
    assert store.get("map-venue").zones == []


def test_zone_count_and_size_are_bounded(store):
    with pytest.raises(ValueError, match="zones"):
        store.save_zones("map-venue", [zone("z%d" % i) for i in range(MAX_ZONES + 1)])
    big = [(i, i % 7) for i in range(MAX_VERTICES_PER_ZONE + 1)]
    with pytest.raises(ValueError, match="vertices"):
        store.save_zones("map-venue", [zone(polygon=big)])


def test_a_failed_zone_write_leaves_the_old_map_and_no_scratch_file(store, tmp_path, monkeypatch):
    store.save_zones("map-venue", [zone("old")])
    real = os.replace

    def failing(src, dst, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(OSError):
        store.save_zones("map-venue", [zone("new")])
    monkeypatch.setattr(os, "replace", real)
    assert [z.zone_id for z in store.get("map-venue").zones] == ["old"]
    assert sorted(os.listdir(tmp_path / "maps" / "map-venue")) == ["floor0", "map.json"]


def test_concurrent_zone_and_status_writes_do_not_lose_each_other(store):
    errors = []

    def zones():
        try:
            for i in range(20):
                store.save_zones("map-venue", [zone("z%d" % i)])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def status():
        try:
            for i in range(20):
                store.set_status("map-venue", "active" if i % 2 == 0 else "draft")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=zones), threading.Thread(target=status)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    final = store.get("map-venue")
    assert final is not None
    assert [z.zone_id for z in final.zones] == ["z19"]  # neither write undid the other
    assert final.status == "draft"


# --- store: status ----------------------------------------------------------


def test_set_status_round_trips(store):
    assert store.set_status("map-venue", "active").status == "active"
    assert store.get("map-venue").status == "active"
    assert store.set_status("map-venue", "archived").status == "archived"
    assert store.set_status("map-venue", "draft").status == "draft"


def test_set_status_keeps_the_zones(store):
    store.save_zones("map-venue", [zone()])
    store.set_status("map-venue", "active")
    assert len(store.get("map-venue").zones) == 1


def test_activating_a_map_requires_at_least_one_floor(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b", map_id="map-empty", floors=0))
    with pytest.raises(ValueError, match="floor"):
        store.set_status("map-empty", "active")
    assert store.get("map-empty").status == "draft"
    # A floorless map may still be archived or left a draft.
    assert store.set_status("map-empty", "archived").status == "archived"


def test_set_status_refuses_a_status_that_is_not_one(store):
    with pytest.raises(ValueError):
        store.set_status("map-venue", "published")  # type: ignore[arg-type]
    assert store.get("map-venue").status == "draft"


def test_set_status_on_an_unknown_map_raises_map_not_found(store):
    with pytest.raises(MapNotFound):
        store.set_status("map-nope", "active")


# --- store: a re-upload -----------------------------------------------------


def test_zones_survive_a_re_upload_of_the_same_map_id(store, tmp_path):
    # Re-surveying a floor must not silently discard the curation done on it.
    store.save_zones("map-venue", [zone()])
    store.save_bundle(make_bundle(tmp_path / "again", map_id="map-venue"))
    assert len(store.get("map-venue").zones) == 1


def test_a_re_upload_drops_an_active_map_back_to_draft(store, tmp_path):
    # The zones were drawn on the old survey; "active" says a person checked
    # them against it, which nobody has done for the new one.
    store.save_zones("map-venue", [zone()])
    store.set_status("map-venue", "active")
    saved = store.save_bundle(make_bundle(tmp_path / "again", floors=2))
    for loaded in (saved, store.get("map-venue")):
        assert loaded.status == "draft"
        assert len(loaded.zones) == 1
        assert len(loaded.floors) == 2


def test_a_re_upload_does_not_keep_an_archived_map_archived(store, tmp_path):
    store.set_status("map-venue", "archived")
    assert store.save_bundle(make_bundle(tmp_path / "again")).status == "draft"


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


def _zone_json(**over):
    return {**{"zone_id": "z1", "map_id": "map-venue", "kind": "no_go", "label": "Stairwell",
               "polygon": [[0, 0], [1, 0], [1, 1]]}, **over}


def test_put_zones_stores_them_and_returns_the_map(client, router):
    response = client.put("/maps/map-venue/zones", json={"zones": [_zone_json()]})
    assert response.status_code == 200
    assert response.json()["zones"][0]["label"] == "Stairwell"
    assert router.store.get("map-venue").zones[0].polygon == [(0, 0), (1, 0), (1, 1)]
    assert client.get("/maps/map-venue").json()["zones"][0]["zone_id"] == "z1"


def test_put_zones_for_an_unknown_map_is_404(client):
    assert client.put("/maps/map-nope/zones", json={"zones": []}).status_code == 404


@pytest.mark.parametrize("body", [
    {"zones": [_zone_json(polygon=[[0, 0], [1, 1]])]},          # two vertices
    {"zones": [_zone_json(kind="wall")]},                        # not a kind
    {"zones": [_zone_json(), _zone_json()]},                     # one id twice
    {"zones": [_zone_json(map_id="map-other")]},                 # another map's zone
    {"zones": "no"}, {},
])
def test_put_zones_refuses_what_is_not_a_zone_list(client, router, body):
    assert client.put("/maps/map-venue/zones", json=body).status_code in (400, 422)
    assert router.store.get("map-venue").zones == []


def test_put_zones_refuses_a_non_finite_vertex(client, router):
    raw = ('{"zones": [{"zone_id": "z1", "map_id": "map-venue", "kind": "no_go", '
           '"label": "x", "polygon": [[0,0],[1,0],[NaN,1]]}]}')
    response = client.put("/maps/map-venue/zones", content=raw,
                          headers={"content-type": "application/json"})
    assert response.status_code in (400, 422)
    assert router.store.get("map-venue").zones == []


def test_put_status_freezes_a_map(client, router):
    response = client.put("/maps/map-venue/status", json={"status": "active"})
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert router.store.get("map-venue").status == "active"


def test_put_status_on_a_map_with_no_floors_is_a_400_with_a_reason(client, router, tmp_path):
    router.store.save_bundle(make_bundle(tmp_path / "e", map_id="map-empty", floors=0))
    response = client.put("/maps/map-empty/status", json={"status": "active"})
    assert response.status_code == 400
    assert "floor" in response.json()["detail"]


def test_put_status_refuses_an_unknown_status_and_an_unknown_map(client):
    assert client.put("/maps/map-venue/status", json={"status": "published"}).status_code == 422
    assert client.put("/maps/map-venue/status", json={}).status_code == 422
    assert client.put("/maps/map-nope/status", json={"status": "active"}).status_code == 404


def test_a_zone_body_over_the_cap_is_refused(client):
    huge = {"zones": [_zone_json(label="x" * (MAX_ZONES_BODY_BYTES + 1))]}
    assert client.put("/maps/map-venue/zones", json=huge).status_code == 413


# --- access rules -----------------------------------------------------------


def test_the_new_routes_have_policy_rules(router):
    routes = {(m, r.path) for r in router.routes if isinstance(r, APIRoute) for m in r.methods}
    assert ("PUT", "/maps/{map_id}/zones") in routes
    assert ("PUT", "/maps/{map_id}/status") in routes
    for key in routes:
        assert key in ROUTES, key


def test_supervisors_edit_zones_and_freeze_but_operators_and_engineers_do_not():
    # Drawing a zone and freezing the map are one judgement -- somebody has
    # looked at it -- and the people who plan patrols against it are supervisors.
    # An engineer uploads the survey (`configure`); a supervisor reviews it.
    for key in [("PUT", "/maps/{map_id}/zones"), ("PUT", "/maps/{map_id}/status")]:
        capabilities = ROUTES[key].capabilities
        assert capabilities == ("editZones",), key
        assert any(role_can("supervisor", c) for c in capabilities)
        for role in ("viewer", "operator", "technician", "engineer"):
            assert not any(role_can(role, c) for c in capabilities), (key, role)


def test_a_bundle_cannot_arrive_already_active(tmp_path):
    # Freezing is a person's review, not something a bundle may claim.
    import json
    bundle = make_bundle(tmp_path / "claims")
    doc = json.loads((bundle / "site_map.json").read_text())
    doc["site_map"]["status"] = "active"
    (bundle / "site_map.json").write_text(json.dumps(doc))
    assert MapStore(tmp_path / "maps").save_bundle(bundle).status == "draft"
