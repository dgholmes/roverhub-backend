"""The engineer-tools switch (2026-11-18): an org owner or admin adds the
engineer's capabilities to their own site role for one session, so the
organisation's owners -- supervisor on every site -- can calibrate the lidar
and start surveys. Against the real app with AUTH_ENABLED switched on."""
from __future__ import annotations

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import api.app as backend
from auth.deps import BridgeBindings, Principal, audit_scope
from auth.router import build_auth_router
from auth.tokens import TokenIssuer
from auth.users import ALL_SITES, User, UserStore
from shared.schemas.version import SCHEMA_VERSION

PASSWORD = "correct horse"
SWITCH = "/auth/engineer-tools"
REFUSED = "only an organisation owner or admin can use engineer tools"
# The four that need `configure` and nothing else, with params their checks accept.
ENGINEER_COMMANDS = {
    "START_SURVEY": {"survey_id": "s-1", "name": "Office"},
    "STOP_SURVEY": {"survey_id": "s-1", "save": True},
    "SET_LIDAR_MOUNT": {"mount": {"x_m": 0.234, "y_m": 0.0, "z_m": 0.12, "pitch_deg": 2.0}},
    "SET_CALIBRATION_MODE": {"mode": "auto"},
}


@pytest.fixture
def auth_on(tmp_path, monkeypatch):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    monkeypatch.setattr(auth, "device_keys", {"rover-a": "device-key-a"})
    monkeypatch.setattr(auth, "broker_service_password", "service-pw")
    monkeypatch.setattr(auth, "bridges", BridgeBindings())
    backend.registry._robots.clear()
    backend.registry.upsert_from_registration("rover-a", "site-a", "quad", schema_version=SCHEMA_VERSION)
    backend.registry.upsert_from_registration("rover-b", "site-b", "quad", schema_version=SCHEMA_VERSION)
    sent = []
    monkeypatch.setattr(backend.mqtt_client, "publish_command", lambda robot_id, command: sent.append(command))
    auth.sent = sent
    yield auth
    backend.registry._robots.clear()


def _user(auth, username, site_roles, org_role="member"):
    return auth.users.create(username=username, display_name=username, password=PASSWORD,
                             org_role=org_role, site_roles=site_roles)


def _login(client, username):
    return client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()


def _headers(session):
    return {"Authorization": f"Bearer {session['access_token']}"}


def _switch(client, session, on):
    return client.post(SWITCH, headers=_headers(session), json={"on": on})


def _command(client, session, type_, robot="rover-a"):
    return client.post(f"/robots/{robot}/commands", headers=_headers(session),
                       json={"type": type_, "label": type_, "params": ENGINEER_COMMANDS[type_],
                             "initiated_by": "someone-else"})


def _owner(auth, client, site_roles=None):
    _user(auth, "olga", site_roles or {ALL_SITES: "supervisor"}, org_role="owner")
    return _login(client, "olga")


# --- the switch --------------------------------------------------------------


def test_sign_in_always_starts_with_it_off(auth_on):
    client = TestClient(backend.app)
    session = _owner(auth_on, client)
    assert session["user"]["engineer_tools"] is False
    assert session["user"]["can_use_engineer_tools"] is True
    assert jwt.decode(session["access_token"], options={"verify_signature": False}).get("eng") is None
    # On, then a fresh sign-in: off again.
    assert _switch(client, session, True).json()["user"]["engineer_tools"] is True
    assert _login(client, "olga")["user"]["engineer_tools"] is False


def test_owner_supervisor_gets_the_engineer_commands_only_while_on(auth_on):
    client = TestClient(backend.app)
    session = _owner(auth_on, client)
    for type_ in ENGINEER_COMMANDS:
        assert _command(client, session, type_).status_code == 403, type_

    on = _switch(client, session, True)
    assert on.status_code == 200
    body = on.json()
    assert set(body) == {"access_token", "token_type", "expires_at", "user"}
    assert body["user"]["engineer_tools"] is True and "password_hash" not in body["user"]
    assert "rh_session" in on.headers.get("set-cookie", "")
    for type_ in ENGINEER_COMMANDS:
        response = _command(client, body, type_)
        assert response.status_code == 200, (type_, response.text)
    assert [c.type for c in auth_on.sent] == list(ENGINEER_COMMANDS)

    off = _switch(client, body, False).json()
    assert off["user"]["engineer_tools"] is False
    for type_ in ENGINEER_COMMANDS:
        assert _command(client, off, type_).status_code == 403, type_


def test_an_admin_may_use_it_too(auth_on):
    client = TestClient(backend.app)
    _user(auth_on, "adam", {"site-a": "supervisor"}, org_role="admin")
    session = _switch(client, _login(client, "adam"), True).json()
    assert _command(client, session, "SET_CALIBRATION_MODE").status_code == 200


@pytest.mark.parametrize("site_roles", [{"site-a": "supervisor"}, {ALL_SITES: "operator"}, {}])
def test_members_cannot_turn_it_on(auth_on, site_roles):
    client = TestClient(backend.app)
    _user(auth_on, "mia", site_roles)
    session = _login(client, "mia")
    assert session["user"]["can_use_engineer_tools"] is False
    for on in (True, False):
        response = _switch(client, session, on)
        assert response.status_code == 403
        assert response.json()["detail"] == REFUSED


def test_a_device_key_or_nobody_cannot_use_it(auth_on):
    client = TestClient(backend.app)
    assert client.post(SWITCH, json={"on": True}).status_code == 401
    assert client.post(SWITCH, headers={"Authorization": "Bearer device-key-a"},
                       json={"on": True}).status_code == 403
    # The session cookie never authorises a POST.
    _owner(auth_on, client)
    assert client.post(SWITCH, json={"on": True}).status_code == 401


def test_it_never_widens_which_sites_the_caller_sees(auth_on):
    client = TestClient(backend.app)
    session = _switch(client, _owner(auth_on, client, {"site-a": "supervisor"}), True).json()
    assert _command(client, session, "SET_LIDAR_MOUNT", robot="rover-a").status_code == 200
    assert _command(client, session, "SET_LIDAR_MOUNT", robot="rover-b").status_code == 403
    robots = client.get("/robots", headers=_headers(session)).json()
    assert {r["robot_id"] for r in robots} == {"rover-a"}
    # And an owner with no site role at all gains nothing anywhere.
    _user(auth_on, "nora", {}, org_role="owner")
    bare = _switch(client, _login(client, "nora"), True).json()
    assert _command(client, bare, "SET_LIDAR_MOUNT").status_code == 403


def test_refresh_keeps_it_and_me_reports_it(auth_on):
    client = TestClient(backend.app)
    session = _switch(client, _owner(auth_on, client), True).json()
    assert client.get("/auth/me", headers=_headers(session)).json()["user"]["engineer_tools"] is True
    refreshed = client.post("/auth/refresh", headers=_headers(session)).json()
    assert refreshed["user"]["engineer_tools"] is True
    assert _command(client, refreshed, "START_SURVEY").status_code == 200
    # Off stays off across a refresh.
    off = _switch(client, refreshed, False).json()
    assert client.post("/auth/refresh", headers=_headers(off)).json()["user"]["engineer_tools"] is False


def test_demotion_ends_it_at_once(auth_on):
    client = TestClient(backend.app)
    session = _switch(client, _owner(auth_on, client), True).json()
    user = auth_on.users.by_username("olga")
    assert _command(client, session, "SET_LIDAR_MOUNT").status_code == 200
    # Demoted while the token is live: it still signs them in, as a supervisor.
    auth_on.users.update(user.user_id, org_role="member")
    assert _command(client, session, "SET_LIDAR_MOUNT").status_code == 403
    me = client.get("/auth/me", headers=_headers(session)).json()["user"]
    assert me["engineer_tools"] is False and me["can_use_engineer_tools"] is False
    refreshed = client.post("/auth/refresh", headers=_headers(session)).json()
    assert refreshed["user"]["engineer_tools"] is False
    assert jwt.decode(refreshed["access_token"], options={"verify_signature": False}).get("eng") is None
    assert _switch(client, refreshed, True).status_code == 403


def test_revocation_ends_it(auth_on):
    client = TestClient(backend.app)
    session = _switch(client, _owner(auth_on, client), True).json()
    auth_on.users.revoke_tokens(auth_on.users.by_username("olga").user_id)
    assert _command(client, session, "SET_LIDAR_MOUNT").status_code == 401
    assert client.post("/auth/refresh", headers=_headers(session)).status_code == 401
    assert _switch(client, session, True).status_code == 401


def test_sign_in_off_answers_not_found_and_the_dev_user_has_no_switch():
    client = TestClient(backend.app)
    assert client.post(SWITCH, json={"on": True}).status_code == 404
    user = client.get("/auth/me").json()["user"]
    assert user["engineer_tools"] is False and user["can_use_engineer_tools"] is False


def test_every_switch_is_audited_both_ways(auth_on):
    rows = []
    app = FastAPI()
    app.include_router(build_auth_router(auth_on, audit=lambda *row: rows.append(row)),
                       dependencies=[Depends(auth_on.guard)])
    client = TestClient(app)
    session = _owner(auth_on, client)
    user = auth_on.users.by_username("olga")
    session = _switch(client, session, True).json()
    _switch(client, session, False)
    assert rows == [("auth.engineer_tools", user.user_id, "ok", {"username": "olga", "on": True}),
                    ("auth.engineer_tools", user.user_id, "ok", {"username": "olga", "on": False})]
    # A refused attempt changes nothing, so there is nothing to record.
    _user(auth_on, "mia", {"site-a": "supervisor"})
    assert _switch(client, _login(client, "mia"), True).status_code == 403
    assert len(rows) == 2


# --- the token and the principal --------------------------------------------


def test_the_claim_is_signed_and_absent_means_off():
    issuer = TokenIssuer("test-secret")
    plain, _ = issuer.issue("u-1", 1)
    eng, _ = issuer.issue("u-1", 1, engineer_tools=True)
    assert issuer.verify(plain).engineer_tools is False
    assert issuer.verify(eng).engineer_tools is True
    # Adding the claim to a token by hand breaks its signature.
    claims = jwt.decode(plain, options={"verify_signature": False})
    forged = jwt.encode({**claims, "eng": True}, "not-the-secret", algorithm="HS256")
    assert issuer.verify(forged) is None


def _principal(org_role, site_roles, engineer_tools=True):
    user = User(user_id="u", username="uu", display_name="u", org_role=org_role,
                site_roles=site_roles, password_hash="x")
    return Principal(kind="user", id="u", user=user, engineer_tools=engineer_tools)


def test_the_principal_adds_engineer_capabilities_only_where_it_holds_a_role():
    p = _principal("owner", {"site-a": "viewer"})
    assert p.can("configure", "site-a") and p.can("edgeTools", "site-a") and p.can("watchLive", "site-a")
    assert not p.can("configure", "site-b")
    assert p.can("configure", None)
    assert not _principal("owner", {}).can("configure", None)
    # Constructed for a member, the flag counts for nothing.
    assert not _principal("member", {"site-a": "viewer"}).can("configure", "site-a")
    assert not _principal("owner", {"site-a": "viewer"}, engineer_tools=False).can("configure", "site-a")


def test_the_audit_log_follows_the_switch():
    # A viewer's site rows need `configure` or `override`: the switch gives the first.
    assert audit_scope(_principal("owner", {"site-a": "viewer"})).allows_site("site-a")
    assert not audit_scope(_principal("owner", {"site-a": "viewer"}, engineer_tools=False)).allows_site("site-a")
    assert not audit_scope(_principal("owner", {"site-a": "viewer"})).allows_site("site-b")
