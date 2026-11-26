"""Real sites (sites/, shared/schemas/sites.py, 2026-11-26): the file store, the
/sites routes and who may use them, the startup backfill, the record a robot's
registration makes for a site nobody has made, and SET_SITE's `_site_known`."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.app as backend
from auth.deps import BridgeBindings
from auth.tokens import TokenIssuer
from auth.users import UserStore
from robots.registry import RobotRegistry
from shared.schemas.sites import Site, SiteCreate, SiteUpdate
from sites.router import build_sites_router
from sites.store import (
    SiteExists, SiteIdNotStorable, SiteStore, new_site_id, site_id_from_name, storable_site_id,
)

PASSWORD = "correct horse"
T0 = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def tick(self, seconds=60):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def store(tmp_path, clock):
    return SiteStore(str(tmp_path / "sites"), clock=clock)


def _create(store, site_id="pict-square", name="PICT Square", timezone_="Asia/Seoul", **extra):
    return store.create(SiteCreate(site_id=site_id, name=name, timezone=timezone_, **extra), site_id)


# ==== the store ===================================================================


def test_store_round_trip(store, tmp_path):
    made = _create(store, address="Teheran-ro 1", location={"lat": 37.5, "lng": 127.0})
    assert made.created_at == made.updated_at == T0
    # A fresh store over the same directory reads exactly what was written.
    again = SiteStore(str(tmp_path / "sites")).get("pict-square")
    assert again == made
    on_disk = json.loads((tmp_path / "sites" / "pict-square.json").read_text(encoding="utf-8"))
    assert Site.model_validate(on_disk) == made
    assert on_disk["location"] == {"lat": 37.5, "lng": 127.0}


def test_store_lists_by_name(store):
    _create(store, "b-site", "beta")
    _create(store, "a-site", "Gamma")
    _create(store, "c-site", "Alpha")
    assert [s.site_id for s in store.list()] == ["c-site", "b-site", "a-site"]


def test_store_get_unknown_and_unstorable_is_none(store):
    assert store.get("nowhere") is None
    for bad in (".", "..", "con", "a.", "a/b", "../x", ""):
        assert store.get(bad) is None


def test_write_is_atomic_a_failed_replace_keeps_the_old_record_and_no_temp_file(store, tmp_path, monkeypatch):
    _create(store)
    directory = tmp_path / "sites"

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.update("pict-square", SiteUpdate(name="Renamed"))
    monkeypatch.undo()
    assert store.get("pict-square").name == "PICT Square"
    assert sorted(os.listdir(directory)) == ["pict-square.json"]


def test_writes_leave_no_temp_files(store, tmp_path):
    _create(store)
    store.update("pict-square", SiteUpdate(name="Renamed"))
    assert sorted(os.listdir(tmp_path / "sites")) == ["pict-square.json"]


def test_a_stray_temp_or_unreadable_file_is_skipped(store, tmp_path):
    _create(store)
    (tmp_path / "sites" / ".abc.tmp").write_text("{", encoding="utf-8")
    (tmp_path / "sites" / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "sites" / "liar.json").write_text(
        _create(SiteStore(str(tmp_path / "other")), "someone-else").model_dump_json(), encoding="utf-8")
    assert [s.site_id for s in store.list()] == ["pict-square"]


def test_create_refuses_a_taken_id_ignoring_case(store):
    _create(store)
    with pytest.raises(SiteExists) as same:
        _create(store, name="Another")
    assert same.value.existing == "pict-square"
    with pytest.raises(SiteExists) as cased:
        _create(store, site_id="PICT-Square")
    assert cased.value.existing == "pict-square"


@pytest.mark.parametrize("bad", [".", "..", "con", "NUL.json", "lpt1", "trailing."])
def test_create_refuses_ids_that_cannot_be_file_names(store, bad):
    with pytest.raises(SiteIdNotStorable):
        store.create(SiteCreate(name="n", timezone="UTC"), bad)


def test_update_merges_and_stamps(store, clock):
    _create(store, address="Old address", location={"lat": 1, "lng": 2})
    clock.tick()
    updated = store.update("pict-square", SiteUpdate(name="Renamed", timezone="UTC"))
    assert (updated.name, updated.timezone, updated.address) == ("Renamed", "UTC", "Old address")
    assert updated.location.lat == 1
    assert updated.created_at == T0 and updated.updated_at == T0 + timedelta(seconds=60)
    assert store.get("pict-square") == updated


def test_update_explicit_null_clears_address_and_location(store):
    _create(store, address="Somewhere", location={"lat": 1, "lng": 2})
    updated = store.update("pict-square", SiteUpdate.model_validate({"address": None, "location": None}))
    assert updated.address is None and updated.location is None
    assert updated.name == "PICT Square"


def test_an_empty_update_changes_nothing(store, clock):
    made = _create(store)
    clock.tick()
    assert store.update("pict-square", SiteUpdate()) == made


def test_update_and_delete_of_an_unknown_site(store):
    assert store.update("nowhere", SiteUpdate(name="x")) is None
    assert store.delete("nowhere") is False
    assert store.delete("..") is False


def test_delete(store, tmp_path):
    _create(store)
    assert store.delete("pict-square") is True
    assert store.get("pict-square") is None
    assert os.listdir(tmp_path / "sites") == []


def test_ensure_makes_a_record_once(store, clock):
    made = store.ensure("site-a")
    assert (made.site_id, made.name, made.timezone) == ("site-a", "site-a", "UTC")
    clock.tick()
    assert store.ensure("site-a") is None
    assert store.get("site-a") == made


def test_ensure_never_overwrites_an_edited_site(store):
    _create(store, "site-a", "Head office")
    assert store.ensure("site-a") is None
    assert store.get("site-a").name == "Head office"


@pytest.mark.parametrize("bad", ["a/b", "+", "", "con", "..", "x" * 65])
def test_ensure_never_raises(store, bad):
    assert store.ensure(bad) is None
    assert store.list() == []


def test_backfill_skips_none_and_duplicates(store):
    made = store.backfill(["site-b", None, "site-a", "site-b", "", "a/b"])
    assert [s.site_id for s in made] == ["site-b", "site-a"]
    assert store.backfill(["site-a", "site-b"]) == []


def test_ids_from_names():
    assert site_id_from_name("PICT Square") == "pict-square"
    assert site_id_from_name("  Café Été -- Lobby #2 ") == "cafe-ete-lobby-2"
    assert site_id_from_name("서울 본사") is None
    assert site_id_from_name("con") is None
    assert len(site_id_from_name("x" * 200)) == 64
    assert new_site_id("PICT Square", None) == "pict-square"
    assert new_site_id("PICT Square", "given") == "given"
    generated = new_site_id("서울 본사", None)
    assert generated.startswith("site-") and len(generated) == 13 and storable_site_id(generated)


# ==== the routes (sign-in off) ===================================================


@pytest.fixture
def api(monkeypatch):
    """The real app with an empty site store, registry and map list."""
    directory = backend.site_store._dir

    def empty():
        for name in os.listdir(directory):
            os.unlink(os.path.join(directory, name))

    empty()
    backend.registry._robots.clear()
    maps = []
    monkeypatch.setattr(backend.maps_router.store, "list", lambda: list(maps))
    client = TestClient(backend.app)
    client.maps = maps
    yield client
    backend.registry._robots.clear()
    empty()


def _post(client, headers=None, **body):
    return client.post("/sites", json=body, headers=headers or {})


def test_list_starts_empty(api):
    assert api.get("/sites").json() == {"items": []}


def test_post_derives_the_id_from_the_name(api):
    response = _post(api, name="  PICT Square ", timezone="Asia/Seoul")
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"site_id", "name", "timezone", "address", "location", "created_at", "updated_at"}
    assert (body["site_id"], body["name"], body["timezone"]) == ("pict-square", "PICT Square", "Asia/Seoul")
    assert body["address"] is None and body["location"] is None
    assert body["created_at"] == body["updated_at"] and body["created_at"].endswith("Z")
    assert api.get("/sites/pict-square").json() == body
    assert api.get("/sites").json() == {"items": [body]}


def test_post_with_an_id_and_everything(api):
    response = _post(api, site_id="HQ_1", name="HQ", timezone="Europe/London", address=" 1 Main St ",
                     location={"lat": 51.5, "lng": -0.12})
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["site_id"], body["address"], body["location"]) == ("HQ_1", "1 Main St", {"lat": 51.5, "lng": -0.12})


def test_post_a_name_with_no_ascii_gets_a_generated_id(api):
    body = _post(api, name="서울 본사", timezone="Asia/Seoul").json()
    assert body["site_id"].startswith("site-") and body["name"] == "서울 본사"


def test_post_a_taken_id_is_409(api):
    assert _post(api, site_id="hq", name="HQ", timezone="UTC").status_code == 201
    again = _post(api, site_id="hq", name="Other", timezone="UTC")
    assert again.status_code == 409 and again.json()["detail"] == "site hq already exists"
    cased = _post(api, site_id="HQ", name="Other", timezone="UTC")
    assert cased.status_code == 409 and "clash with site hq" in cased.json()["detail"]


def test_post_a_taken_derived_id_is_409_and_says_how_to_fix_it(api):
    assert _post(api, name="PICT Square", timezone="UTC").status_code == 201
    again = _post(api, name="pict  square!", timezone="UTC")
    assert again.status_code == 409
    assert again.json()["detail"] == "site pict-square already exists; give it another name or choose its ID"


@pytest.mark.parametrize("body", [
    {"name": "", "timezone": "UTC"},
    {"name": "   ", "timezone": "UTC"},
    {"name": "x" * 81, "timezone": "UTC"},
    {"timezone": "UTC"},
    {"name": "n"},
    {"name": "n", "timezone": "Mars/Base"},
    {"name": "n", "timezone": "+09:00"},
    {"name": "n", "timezone": "UTC", "location": {"lat": 95, "lng": 0}},
    {"name": "n", "timezone": "UTC", "location": {"lat": 0, "lng": -200}},
    {"name": "n", "timezone": "UTC", "location": {"lat": 1}},
    {"name": "n", "timezone": "UTC", "address": "a" * 201},
    {"name": "n", "timezone": "UTC", "site_id": "a/b"},
    {"name": "n", "timezone": "UTC", "site_id": "x" * 65},
])
def test_post_bad_bodies_are_422(api, body):
    response = api.post("/sites", json=body)
    assert response.status_code == 422, response.text
    assert api.get("/sites").json() == {"items": []}


@pytest.mark.parametrize("site_id", [".", "..", "con", "a."])
def test_post_an_id_that_cannot_be_a_file_name_is_422(api, site_id):
    response = _post(api, site_id=site_id, name="n", timezone="UTC")
    assert response.status_code == 422
    assert "cannot be used as a site ID" in response.json()["detail"]


def test_post_bodies_that_are_not_objects_are_422(api):
    assert api.post("/sites", content=b"[1]", headers={"content-type": "application/json"}).status_code == 422
    assert api.post("/sites", content=b"{oops", headers={"content-type": "application/json"}).status_code == 422


def test_post_a_huge_body_is_413(api):
    response = _post(api, name="n", timezone="UTC", filler="x" * 20000)
    assert response.status_code == 413


def test_get_unknown_is_404(api):
    response = api.get("/sites/nowhere")
    assert response.status_code == 404 and response.json()["detail"] == "site nowhere not found"


def test_patch_renames_and_keeps_the_rest(api):
    made = _post(api, site_id="hq", name="HQ", timezone="UTC", address="1 Main St").json()
    response = api.patch("/sites/hq", json={"name": "Head office"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["name"], body["timezone"], body["address"]) == ("Head office", "UTC", "1 Main St")
    assert body["created_at"] == made["created_at"]
    assert api.get("/sites/hq").json() == body


def test_patch_explicit_null_clears_and_absence_keeps(api):
    _post(api, site_id="hq", name="HQ", timezone="UTC", address="1 Main St", location={"lat": 1, "lng": 2})
    kept = api.patch("/sites/hq", json={"timezone": "Asia/Seoul"}).json()
    assert kept["address"] == "1 Main St" and kept["location"] == {"lat": 1, "lng": 2}
    cleared = api.patch("/sites/hq", json={"address": None, "location": None}).json()
    assert cleared["address"] is None and cleared["location"] is None
    assert cleared["timezone"] == "Asia/Seoul"


def test_patch_sets_a_location(api):
    _post(api, site_id="hq", name="HQ", timezone="UTC")
    body = api.patch("/sites/hq", json={"location": {"lat": -33.86, "lng": 151.2}}).json()
    assert body["location"] == {"lat": -33.86, "lng": 151.2}


def test_patch_cannot_change_the_id(api):
    _post(api, site_id="hq", name="HQ", timezone="UTC")
    response = api.patch("/sites/hq", json={"site_id": "elsewhere", "name": "x"})
    assert response.status_code == 422 and response.json()["detail"] == "site_id cannot change"
    # Naming its own ID is harmless.
    assert api.patch("/sites/hq", json={"site_id": "hq", "name": "x"}).status_code == 200
    assert api.get("/sites/elsewhere").status_code == 404


@pytest.mark.parametrize("body", [
    {"name": None}, {"timezone": None}, {"name": ""}, {"timezone": "nowhere/at-all"},
    {"location": {"lat": 100, "lng": 0}}, {"address": "a" * 201},
])
def test_patch_bad_bodies_are_422(api, body):
    _post(api, site_id="hq", name="HQ", timezone="UTC")
    assert api.patch("/sites/hq", json=body).status_code == 422
    assert api.get("/sites/hq").json()["name"] == "HQ"


def test_patch_unknown_is_404(api):
    assert api.patch("/sites/nowhere", json={"name": "x"}).status_code == 404


def test_delete_then_404(api):
    _post(api, site_id="hq", name="HQ", timezone="UTC")
    assert api.delete("/sites/hq").status_code == 204
    assert api.get("/sites/hq").status_code == 404
    assert api.delete("/sites/hq").status_code == 404


def test_delete_refused_while_a_robot_is_registered_there(api):
    _post(api, site_id="hq", name="HQ", timezone="UTC")
    backend.registry.upsert_from_registration("rover-a", "hq", "quad")
    backend.registry.upsert_from_registration("rover-b", "hq", "quad")
    backend.registry.mark_offline("rover-b")  # offline still counts: it is registered there
    response = api.delete("/sites/hq")
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "site hq is in use: robots rover-a, rover-b are registered there (move or remove them first)")
    assert api.get("/sites/hq").status_code == 200


def test_delete_refused_while_a_map_belongs_to_it(api):
    _post(api, site_id="hq", name="HQ", timezone="UTC")
    api.maps.append(SimpleNamespace(map_id="map-1", name="Ground floor", site_id="hq"))
    api.maps.append(SimpleNamespace(map_id="map-2", name="Lobby", site_id="elsewhere"))
    response = api.delete("/sites/hq")
    assert response.status_code == 409
    assert response.json()["detail"] == "site hq is in use: map Ground floor (map-1) belongs to it (delete them first)"


def test_delete_refused_while_someone_holds_a_role_there(api, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.auth, "users", UserStore(str(tmp_path / "users.json")))
    backend.auth.users.create(username="ana", display_name="Ana", password=PASSWORD, site_roles={"hq": "viewer"})
    backend.auth.users.create(username="all", display_name="All", password=PASSWORD, site_roles={"*": "viewer"})
    _post(api, site_id="hq", name="HQ", timezone="UTC")
    response = api.delete("/sites/hq")
    assert response.status_code == 409
    assert "person ana holds a role there" in response.json()["detail"]


def test_delete_names_every_reason(api, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.auth, "users", UserStore(str(tmp_path / "users.json")))
    backend.auth.users.create(username="ana", display_name="Ana", password=PASSWORD, site_roles={"hq": "viewer"})
    _post(api, site_id="hq", name="HQ", timezone="UTC")
    backend.registry.upsert_from_registration("rover-a", "hq", "quad")
    api.maps.append(SimpleNamespace(map_id="map-1", name="map-1", site_id="hq"))
    detail = api.delete("/sites/hq").json()["detail"]
    assert detail == ("site hq is in use: robot rover-a is registered there (move or remove them first); "
                      "map map-1 belongs to it (delete them first); "
                      "person ana holds a role there (remove it on the Team page first)")


def test_changes_are_audited(tmp_path):
    recorded = []
    app = FastAPI()
    app.include_router(build_sites_router(
        SiteStore(str(tmp_path / "sites")),
        audit=lambda action, target, outcome, detail=None, **kw: recorded.append((action, target, detail, kw))))
    client = TestClient(app)
    client.post("/sites", json={"site_id": "hq", "name": "HQ", "timezone": "UTC"})
    client.patch("/sites/hq", json={"name": "Head office", "address": None})
    client.patch("/sites/hq", json={})  # changes nothing: not audited
    client.delete("/sites/hq")
    assert recorded == [
        ("site.create", "hq", {"name": "HQ", "timezone": "UTC"}, {"site_id": "hq"}),
        ("site.update", "hq", {"changed": ["address", "name"], "previous_name": "HQ"}, {"site_id": "hq"}),
        ("site.delete", "hq", {"name": "Head office"}, {"site_id": "hq"}),
    ]


# ==== who may (sign-in on) =======================================================


@pytest.fixture
def auth_on(tmp_path, monkeypatch, api):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    monkeypatch.setattr(auth, "device_keys", {"rover-a": "device-key-a"})
    monkeypatch.setattr(auth, "bridges", BridgeBindings())
    people = {
        "own": dict(org_role="owner", site_roles={"*": "supervisor"}),
        "adm": dict(org_role="admin", site_roles={}),
        "ana": dict(site_roles={"site-a": "viewer"}),
        "eng": dict(site_roles={"site-a": "engineer", "site-b": "engineer"}),
        "all": dict(site_roles={"*": "viewer"}),
        "nob": dict(site_roles={}),
    }
    for name, spec in people.items():
        auth.users.create(username=name, display_name=name, password=PASSWORD, **spec)
    for site_id in ("site-a", "site-b", "site-c"):
        backend.site_store.create(SiteCreate(name=site_id.upper(), timezone="UTC"), site_id)

    def headers(name):
        token = api.post("/auth/login", json={"username": name, "password": PASSWORD}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return headers


def _ids(response):
    assert response.status_code == 200, response.text
    return [site["site_id"] for site in response.json()["items"]]


def test_signed_out_is_401(api, auth_on):
    assert api.get("/sites").status_code == 401
    assert api.get("/sites/site-a").status_code == 401
    assert _post(api, name="n", timezone="UTC").status_code == 401


def test_a_device_key_may_not_read_sites(api, auth_on):
    device = {"Authorization": "Bearer device-key-a"}
    assert api.get("/sites", headers=device).status_code == 403
    assert _post(api, device, name="n", timezone="UTC").status_code == 403


def test_list_shows_each_person_their_sites(api, auth_on):
    assert _ids(api.get("/sites", headers=auth_on("own"))) == ["site-a", "site-b", "site-c"]
    # An admin with no site role still sees every site: they manage them.
    assert _ids(api.get("/sites", headers=auth_on("adm"))) == ["site-a", "site-b", "site-c"]
    assert _ids(api.get("/sites", headers=auth_on("all"))) == ["site-a", "site-b", "site-c"]
    assert _ids(api.get("/sites", headers=auth_on("eng"))) == ["site-a", "site-b"]
    assert _ids(api.get("/sites", headers=auth_on("ana"))) == ["site-a"]
    assert _ids(api.get("/sites", headers=auth_on("nob"))) == []


def test_get_one_hides_a_site_the_caller_may_not_see(api, auth_on):
    ana = auth_on("ana")
    assert api.get("/sites/site-a", headers=ana).status_code == 200
    hidden = api.get("/sites/site-b", headers=ana)
    missing = api.get("/sites/site-zzz", headers=ana)
    assert hidden.status_code == missing.status_code == 404
    assert api.get("/sites/site-b", headers=auth_on("adm")).status_code == 200


@pytest.mark.parametrize("who", ["ana", "eng", "all", "nob"])
def test_only_org_owners_and_admins_change_sites(api, auth_on, who):
    headers = auth_on(who)
    for response in (
        _post(api, headers, name="New", timezone="UTC"),
        api.patch("/sites/site-a", json={"name": "x"}, headers=headers),
        api.delete("/sites/site-c", headers=headers),
    ):
        assert response.status_code == 403
        assert response.json()["detail"] == "only an organisation owner or admin can do this"
    assert _ids(api.get("/sites", headers=auth_on("own"))) == ["site-a", "site-b", "site-c"]
    assert api.get("/sites/site-a", headers=auth_on("own")).json()["name"] == "SITE-A"


@pytest.mark.parametrize("who", ["own", "adm"])
def test_org_owners_and_admins_change_sites(api, auth_on, who):
    headers = auth_on(who)
    assert _post(api, headers, name="New site", timezone="UTC").status_code == 201
    assert api.patch("/sites/new-site", json={"name": "Renamed"}, headers=headers).json()["name"] == "Renamed"
    assert api.delete("/sites/site-c", headers=headers).status_code == 204


# ==== the startup backfill ========================================================


def test_backfill_makes_a_record_for_every_known_site_id(api, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.auth, "users", UserStore(str(tmp_path / "users.json")))
    backend.auth.users.create(username="ana", display_name="Ana", password=PASSWORD,
                              site_roles={"site-role": "viewer", "*": "viewer"})
    backend.registry.upsert_from_registration("rover-a", "site-robot", "quad")
    api.maps.append(SimpleNamespace(map_id="map-1", name="m", site_id="site-map"))
    # One already exists and has been edited: it is left alone.
    backend.site_store.create(SiteCreate(name="Head office", timezone="Asia/Seoul"), "site-map")

    made = backend.backfill_sites()

    assert sorted(s.site_id for s in made) == ["site-role"]  # site-robot was made on registration
    for site_id in ("site-robot", "site-role"):
        site = backend.site_store.get(site_id)
        assert (site.name, site.timezone, site.address, site.location) == (site_id, "UTC", None, None)
    assert backend.site_store.get("site-map").name == "Head office"
    assert backend.site_store.get("*") is None
    assert backend.backfill_sites() == []


def test_backfill_covers_robots_registered_before_any_hook(api, monkeypatch):
    # A registry entry that never went through the hook (e.g. a hook that
    # failed): the backfill still gives its site a record.
    monkeypatch.setattr(backend.registry, "on_site", None)
    backend.registry.upsert_from_registration("rover-a", "site-quiet", "quad")
    assert backend.site_store.get("site-quiet") is None
    assert "site-quiet" in [s.site_id for s in backend.backfill_sites()]


def test_startup_runs_the_backfill(api, monkeypatch):
    monkeypatch.setattr(backend.mqtt_client, "connect", lambda: None)
    monkeypatch.setattr(backend.mqtt_client, "disconnect", lambda: None)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    api.maps.append(SimpleNamespace(map_id="map-1", name="m", site_id="site-from-a-map"))
    with TestClient(backend.app) as client:
        assert client.get("/sites/site-from-a-map").json()["timezone"] == "UTC"


def test_a_failing_backfill_does_not_stop_startup(api, monkeypatch):
    monkeypatch.setattr(backend.mqtt_client, "connect", lambda: None)
    monkeypatch.setattr(backend.mqtt_client, "disconnect", lambda: None)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    def boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(backend, "backfill_sites", boom)
    with TestClient(backend.app) as client:
        assert client.get("/healthz").status_code == 200


# ==== lazy creation on registration =============================================


def _register(robot_id, site_id):
    payload = {"bridge_id": f"bridge-{robot_id}", "site_id": site_id, "robot_id": robot_id, "robot_type": "quad"}
    backend.mqtt_client.handle_message(f"roverhub/admin/bridge-{robot_id}/register", json.dumps(payload).encode())


def test_a_registration_at_an_unknown_site_makes_its_record(api):
    _register("rover-a", "site-new")
    site = api.get("/sites/site-new").json()
    assert (site["name"], site["timezone"]) == ("site-new", "UTC")
    assert backend.registry.get("rover-a").site_id == "site-new"


def test_repeated_registrations_leave_the_record_alone(api):
    _register("rover-a", "site-new")
    api.patch("/sites/site-new", json={"name": "Renamed"})
    _register("rover-a", "site-new")
    backend.registry._robots.clear()  # as after a backend restart
    _register("rover-a", "site-new")
    assert api.get("/sites/site-new").json()["name"] == "Renamed"


def test_a_robot_that_moves_makes_a_record_for_its_new_site(api):
    _register("rover-a", "site-one")
    _register("rover-a", "site-two")
    assert api.get("/sites/site-two").status_code == 200


def test_registry_tells_on_site_only_for_a_new_robot_or_a_new_site():
    seen = []
    registry = RobotRegistry(on_site=seen.append)
    registry.upsert_from_registration("rover-a", "site-a", "quad", bridge_id="b-a")
    registry.upsert_from_registration("rover-a", "site-a", "quad", "b-a", "0.0.32")  # positional, as tests do
    registry.upsert_from_registration("rover-b", "site-a", "quad")
    registry.upsert_from_registration("rover-a", "site-b", "quad")
    assert seen == ["site-a", "site-a", "site-b"]
    assert registry.get("rover-a").schema_version is None  # the last registration replaced it


def test_a_failing_on_site_never_costs_the_registration():
    def boom(site_id):
        raise RuntimeError("disk on fire")

    registry = RobotRegistry(on_site=boom)
    registry.upsert_from_registration("rover-a", "site-a", "quad")
    assert registry.get("rover-a").site_id == "site-a"


def test_a_registration_at_a_site_id_no_file_can_hold_still_registers(api):
    _register("rover-a", "con")
    assert backend.registry.get("rover-a").site_id == "con"
    assert backend.site_store.list() == []


# ==== SET_SITE's _site_known ====================================================


def test_site_known_reads_the_store_first(api):
    backend.site_store.create(SiteCreate(name="Only a record", timezone="UTC"), "site-record")
    assert backend._site_known("site-record") is True
    assert backend._site_known("site-nowhere") is False


def test_site_known_falls_back_to_what_names_a_site(api, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.auth, "users", UserStore(str(tmp_path / "users.json")))
    backend.auth.users.create(username="ana", display_name="Ana", password=PASSWORD,
                              site_roles={"site-role": "viewer"})
    api.maps.append(SimpleNamespace(map_id="map-1", name="m", site_id="site-map"))
    monkeypatch.setattr(backend.registry, "on_site", None)
    backend.registry.upsert_from_registration("rover-a", "site-robot", "quad")
    assert backend.site_store.list() == []
    for site_id in ("site-role", "site-map", "site-robot"):
        assert backend._site_known(site_id) is True


def test_set_site_to_a_site_with_only_a_record_is_accepted(api, monkeypatch):
    sent = []
    monkeypatch.setattr(backend.mqtt_client, "publish_command", lambda robot_id, command: sent.append(command))
    payload = {"bridge_id": "bridge-rover-a", "site_id": "site-a", "robot_id": "rover-a", "robot_type": "quad",
               "schema_version": "0.0.32"}
    backend.mqtt_client.handle_message("roverhub/admin/bridge-rover-a/register", json.dumps(payload).encode())
    backend.site_store.create(SiteCreate(name="Made in the console", timezone="UTC"), "site-console")
    response = api.post("/robots/rover-a/commands", json={
        "type": "SET_SITE", "label": "Move", "params": {"site_id": "site-console"}, "initiated_by": "x"})
    assert response.status_code == 200, response.text
    assert [c.type for c in sent] == ["SET_SITE"]
