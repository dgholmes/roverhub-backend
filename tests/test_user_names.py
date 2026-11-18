"""GET /users/names: display names for ids, for histories (2026-11-18)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.app as backend
from auth.deps import DEV_USER_ID
from auth.policy import ROUTES, Rule
from auth.router import MAX_NAME_IDS
from auth.tokens import TokenIssuer
from auth.users import UserStore

PASSWORD = "correct horse"


@pytest.fixture
def auth_on(tmp_path, monkeypatch):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    monkeypatch.setattr(auth, "device_keys", {"rover-a": "device-key-a"})
    yield auth


def _bearer(client, username):
    token = client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_the_rule_is_signed_in():
    assert ROUTES[("GET", "/users/names")] == Rule(signed_in=True)


def test_names_for_ids_unknown_left_out_names_only(auth_on):
    viewer = auth_on.users.create(username="vee", display_name="Vee Viewer", password=PASSWORD,
                                  org_role="member", site_roles={"site-a": "viewer"})
    plain = auth_on.users.create(username="plain", display_name="", password=PASSWORD,
                                 org_role="member", site_roles={})
    gone = auth_on.users.create(username="gone", display_name="Gone Person", password=PASSWORD,
                                org_role="member", site_roles={})
    auth_on.users.update(gone.user_id, disabled=True)
    client = TestClient(backend.app)
    ids = ",".join([viewer.user_id, plain.user_id, gone.user_id, "nobody", viewer.user_id, ""])
    r = client.get("/users/names", params={"ids": ids}, headers=_bearer(client, "vee"))
    assert r.status_code == 200
    # a viewer may ask; display name, else username; a disabled user's name stays
    assert r.json() == {"names": {viewer.user_id: "Vee Viewer", plain.user_id: "plain",
                                  gone.user_id: "Gone Person"}}


def test_no_ids_is_no_names(auth_on):
    auth_on.users.create(username="vee", display_name="Vee", password=PASSWORD, org_role="member",
                         site_roles={})
    client = TestClient(backend.app)
    assert client.get("/users/names", headers=_bearer(client, "vee")).json() == {"names": {}}


def test_at_most_50_ids(auth_on):
    auth_on.users.create(username="vee", display_name="Vee", password=PASSWORD, org_role="member",
                         site_roles={})
    client = TestClient(backend.app)
    headers = _bearer(client, "vee")
    ok = ",".join(f"u{i}" for i in range(MAX_NAME_IDS))
    assert client.get("/users/names", params={"ids": ok}, headers=headers).status_code == 200
    too_many = ok + ",one-more"
    assert client.get("/users/names", params={"ids": too_many}, headers=headers).status_code == 400
    assert MAX_NAME_IDS == 50


def test_signed_out_and_devices_are_refused(auth_on):
    client = TestClient(backend.app)
    assert client.get("/users/names", params={"ids": "x"}).status_code == 401
    r = client.get("/users/names", params={"ids": "x"},
                   headers={"Authorization": "Bearer device-key-a"})
    assert r.status_code == 403


def test_sign_in_off_names_only_the_dev_user():
    assert backend.auth.enabled is False
    client = TestClient(backend.app)
    r = client.get("/users/names", params={"ids": f"{DEV_USER_ID},someone"})
    assert r.status_code == 200 and r.json() == {"names": {DEV_USER_ID: "Developer"}}
    assert client.get("/users/names", params={"ids": "someone"}).json() == {"names": {}}
