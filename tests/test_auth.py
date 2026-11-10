"""Sign-in, role checks and the broker hooks, against the real app with
AUTH_ENABLED switched on for the test."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api.app as backend
from auth.deps import DEV_USER_ID
from auth.mqtt_hooks import ACC_READ, ACC_SUBSCRIBE, ACC_WRITE
from auth.policy import CAPABILITIES, ROUTES
from auth.router import LoginThrottle
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
    monkeypatch.setattr(auth, "broker_service_password", "service-pw")
    backend.registry._robots.clear()
    backend.registry.upsert_from_registration("rover-a", "site-a", "quad")
    backend.registry.upsert_from_registration("rover-b", "site-b", "quad")
    monkeypatch.setattr(backend.mqtt_client, "publish_command", lambda robot_id, command: None)
    yield auth
    backend.registry._robots.clear()


def _user(auth, username, site_roles, org_role="member"):
    return auth.users.create(username=username, display_name=username, password=PASSWORD,
                             org_role=org_role, site_roles=site_roles)


def _login(client, username, password=PASSWORD):
    return client.post("/auth/login", json={"username": username, "password": password})


def _bearer(client, username):
    token = _login(client, username).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _command(client, headers, robot="rover-a", type_="DRIVE", params=None):
    return client.post(f"/robots/{robot}/commands", headers=headers,
                       json={"type": type_, "label": type_, "params": {} if params is None else params,
                             "initiated_by": "someone-else"})


# --- every route has a rule -------------------------------------------------


def test_every_route_has_an_access_rule():
    missing = [
        (method, route.path)
        for route in backend.app.routes if isinstance(route, APIRoute)
        for method in route.methods
        if (method, route.path) not in ROUTES
    ]
    assert missing == []


def test_a_route_without_a_rule_is_refused_when_sign_in_is_on(auth_on):
    @backend.app.get("/__unruled")
    def _unruled():
        return {"open": True}
    try:
        assert TestClient(backend.app).get("/__unruled").status_code == 403
    finally:
        backend.app.router.routes = [r for r in backend.app.router.routes if getattr(r, "path", "") != "/__unruled"]


# --- signing in -------------------------------------------------------------


def test_sign_in_off_keeps_everything_open_and_reports_the_dev_user():
    client = TestClient(backend.app)
    assert client.get("/robots").status_code == 200
    me = client.get("/auth/me").json()
    assert me["auth_enabled"] is False and me["user"]["user_id"] == DEV_USER_ID
    assert client.get("/auth/config").json() == {"auth_enabled": False, "needs_first_owner": False}


def test_signed_out_requests_are_refused(auth_on):
    client = TestClient(backend.app)
    assert client.get("/robots").status_code == 401
    assert client.get("/healthz").status_code == 200
    assert client.get("/auth/config").json() == {"auth_enabled": True, "needs_first_owner": True}


def test_login_returns_a_token_that_works_and_never_the_hash(auth_on):
    _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    body = _login(client, "ANA ").json()
    assert body["user"]["username"] == "ana"
    assert "password_hash" not in body["user"]
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/auth/me", headers=headers).json()["user"]["username"] == "ana"
    assert client.get("/robots", headers=headers).status_code == 200


def test_wrong_password_and_unknown_user_look_the_same(auth_on):
    _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    wrong = _login(client, "ana", "nope-nope")
    unknown = _login(client, "nobody", "nope-nope")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_repeated_wrong_passwords_lock_the_username_for_a_while():
    now = [0.0]
    throttle = LoginThrottle(limit=3, window_s=60, lock_s=30, clock=lambda: now[0])
    for _ in range(3):
        throttle.failed("ana")
    assert throttle.retry_after("ana") == 30
    assert throttle.retry_after("ben") == 0
    now[0] = 31
    assert throttle.retry_after("ana") == 0


def test_the_session_cookie_authorises_reads_but_never_writes(auth_on):
    _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    _login(client, "ana")  # TestClient keeps the cookie
    assert client.get("/robots").status_code == 200
    assert _command(client, {}).status_code == 401


def test_changing_the_password_ends_existing_sessions(auth_on):
    _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    headers = _bearer(client, "ana")
    response = client.post("/auth/password", headers=headers,
                           json={"current_password": PASSWORD, "new_password": "a new password"})
    assert response.status_code == 204
    assert client.get("/robots", headers=headers).status_code == 401
    assert _login(client, "ana", "a new password").status_code == 200


def test_a_disabled_user_is_signed_out(auth_on):
    user = _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    headers = _bearer(client, "ana")
    auth_on.users.update(user.user_id, disabled=True)
    assert client.get("/robots", headers=headers).status_code == 401
    assert _login(client, "ana").status_code == 401


def test_an_expired_token_is_refused(auth_on, monkeypatch):
    user = _user(auth_on, "ana", {"site-a": "operator"})
    token, _ = TokenIssuer("test-secret", ttl_s=10, clock=lambda: 1000).issue(user.user_id, user.token_version)
    assert auth_on.user_for_token(token) is None


def test_refresh_extends_a_live_session_and_keeps_revocation(auth_on):
    user = _user(auth_on, "ana", {"site-a": "operator"})
    clock = {"t": 1000.0}
    auth_on.issuer = TokenIssuer("test-secret", ttl_s=100, clock=lambda: clock["t"])
    client = TestClient(backend.app)
    first = _login(client, "ana").json()
    clock["t"] = 1060.0
    r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {first['access_token']}"})
    assert r.status_code == 200
    body = r.json()
    assert body["expires_at"] == 1160 > first["expires_at"]
    assert body["user"]["user_id"] == user.user_id and "password_hash" not in body["user"]
    assert "rh_session" in r.headers.get("set-cookie", "")
    # Past the first token's expiry, the refreshed one still works.
    clock["t"] = 1120.0
    assert client.get("/robots", headers={"Authorization": f"Bearer {body['access_token']}"}).status_code == 200
    assert client.post("/auth/refresh",
                       headers={"Authorization": f"Bearer {first['access_token']}"}).status_code == 401
    # Signing out everywhere ends the refreshed token too: refresh never outlives revocation.
    auth_on.users.revoke_tokens(user.user_id)
    assert client.post("/auth/refresh",
                       headers={"Authorization": f"Bearer {body['access_token']}"}).status_code == 401


def test_refresh_needs_a_user_session(auth_on):
    client = TestClient(backend.app)
    assert client.post("/auth/refresh").status_code == 401
    assert client.post("/auth/refresh", headers={"Authorization": "Bearer device-key-a"}).status_code == 403
    # The cookie alone never authorises a POST, so another page cannot keep a session alive.
    _user(auth_on, "ana", {"site-a": "operator"})
    _login(client, "ana")
    assert client.post("/auth/refresh").status_code == 401


def test_refresh_is_not_found_with_sign_in_off():
    assert TestClient(backend.app).post("/auth/refresh").status_code == 404


# --- roles ------------------------------------------------------------------


def test_a_viewer_can_watch_but_not_command(auth_on):
    _user(auth_on, "vic", {"site-a": "viewer"})
    client = TestClient(backend.app)
    headers = _bearer(client, "vic")
    assert client.get("/robots", headers=headers).status_code == 200
    assert _command(client, headers, type_="ESTOP").status_code == 403


def test_an_operator_drives_only_at_their_own_site(auth_on):
    _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    headers = _bearer(client, "ana")
    assert _command(client, headers, robot="rover-a").status_code == 200
    assert _command(client, headers, robot="rover-b").status_code == 403


def test_a_technician_may_stop_and_recover_but_not_drive(auth_on):
    _user(auth_on, "tom", {"site-a": "technician"})
    client = TestClient(backend.app)
    headers = _bearer(client, "tom")
    assert _command(client, headers, type_="ESTOP").status_code == 200
    assert _command(client, headers, type_="RESET_ESTOP").status_code == 200
    assert _command(client, headers, type_="DRIVE").status_code == 403


_NAV_GOAL = {"x_m": 1.0, "y_m": 2.0, "yaw_rad": None, "map_id": "m", "floor_id": "f"}


def test_a_nav_goal_needs_drive_but_cancelling_it_only_needs_estop(auth_on):
    # Walking the robot to a point is driving it; stopping that walk is never
    # harder than moving, so anyone who may E-Stop may cancel the goal.
    _user(auth_on, "ana", {"site-a": "operator"})
    _user(auth_on, "tom", {"site-a": "technician"})
    _user(auth_on, "vic", {"site-a": "viewer"})
    client = TestClient(backend.app)
    ana, tom, vic = (_bearer(client, n) for n in ("ana", "tom", "vic"))
    assert _command(client, ana, type_="NAV_GOTO", params=_NAV_GOAL).status_code == 200
    assert _command(client, ana, type_="NAV_CANCEL").status_code == 200
    assert _command(client, tom, type_="NAV_GOTO", params=_NAV_GOAL).status_code == 403
    assert _command(client, tom, type_="NAV_CANCEL").status_code == 200
    assert _command(client, vic, type_="NAV_GOTO", params=_NAV_GOAL).status_code == 403
    assert _command(client, vic, type_="NAV_CANCEL").status_code == 403


def test_a_command_is_attributed_to_the_signed_in_user_whatever_the_body_says(auth_on):
    user = _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    command = _command(client, _bearer(client, "ana")).json()
    assert command["initiated_by"] == user.user_id


def test_planning_needs_a_supervisor(auth_on):
    _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    response = client.delete("/missions/m-1", headers=_bearer(client, "ana"))
    assert response.status_code == 403


def test_an_all_sites_role_reaches_every_site(auth_on):
    _user(auth_on, "own", {"*": "supervisor"}, org_role="owner")
    client = TestClient(backend.app)
    headers = _bearer(client, "own")
    assert _command(client, headers, robot="rover-b").status_code == 200


# --- edge devices -----------------------------------------------------------


def test_a_device_key_works_only_for_its_own_robot_and_device_routes(auth_on):
    client = TestClient(backend.app)
    headers = {"Authorization": "Bearer device-key-a"}
    assert client.get("/audio-clips/nope", headers=headers).status_code not in (401, 403)
    assert client.post("/robots/rover-b/snapshots", headers=headers).status_code == 403
    assert client.get("/robots", headers=headers).status_code == 403
    assert _command(client, headers).status_code == 403


# --- user management --------------------------------------------------------


def test_unkeyed_bridges_reach_only_device_routes_when_allowed(auth_on, monkeypatch):
    client = TestClient(backend.app)
    assert client.post("/runs/run-x/events", json=[]).status_code == 401
    monkeypatch.setattr(auth_on, "allow_unkeyed_bridges", True)
    assert client.post("/runs/run-x/events", json=[]).status_code not in (401, 403)
    # Everything a person does still needs signing in.
    assert client.get("/robots").status_code == 401
    assert _command(client, {}).status_code == 401
    assert client.get("/users").status_code == 401


def test_members_cannot_manage_users(auth_on):
    _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    assert client.get("/users", headers=_bearer(client, "ana")).status_code == 403


def test_an_admin_can_add_members_but_not_owners(auth_on):
    _user(auth_on, "adm", {}, org_role="admin")
    client = TestClient(backend.app)
    headers = _bearer(client, "adm")
    new = {"username": "ben", "password": PASSWORD, "site_roles": {"site-a": "viewer"}}
    assert client.post("/users", headers=headers, json=new).status_code == 201
    assert client.post("/users", headers=headers, json={**new, "username": "boss", "org_role": "owner"}).status_code == 403
    assert all("password_hash" not in u for u in client.get("/users", headers=headers).json())


def test_the_last_owner_cannot_be_demoted_or_disabled(auth_on):
    owner = _user(auth_on, "own", {"*": "supervisor"}, org_role="owner")
    client = TestClient(backend.app)
    headers = _bearer(client, "own")
    assert client.patch(f"/users/{owner.user_id}", headers=headers, json={"org_role": "admin"}).status_code == 409
    assert client.patch(f"/users/{owner.user_id}", headers=headers, json={"disabled": True}).status_code == 409


def test_short_passwords_are_refused(tmp_path):
    with pytest.raises(ValueError):
        UserStore(str(tmp_path / "u.json")).create(username="ana", display_name="", password="short")


def test_users_survive_a_restart_and_the_file_holds_no_plain_password(tmp_path):
    path = tmp_path / "users.json"
    UserStore(str(path)).create(username="ana", display_name="Ana", password=PASSWORD)
    assert PASSWORD not in path.read_text()
    assert UserStore(str(path)).check_credentials("ana", PASSWORD) is not None


# --- broker hooks -----------------------------------------------------------


def _acl(client, username, topic, acc):
    return client.post("/mqtt/acl", json={"username": username, "clientid": "c", "topic": topic, "acc": acc}).status_code


def test_broker_logins(auth_on):
    user = _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    token = _login(client, "ana").json()["access_token"]
    login = lambda u, p: client.post("/mqtt/auth", json={"username": u, "password": p}).status_code  # noqa: E731
    assert login("roverhub-backend", "service-pw") == 200
    assert login("roverhub-backend", "wrong") == 403
    assert login("rover-a", "device-key-a") == 200
    assert login("rover-b", "device-key-a") == 403
    assert login(user.user_id, token) == 200
    assert login("someone-else", token) == 403
    assert client.post("/mqtt/superuser", json={"username": "roverhub-backend"}).status_code == 200
    assert client.post("/mqtt/superuser", json={"username": user.user_id}).status_code == 403


def test_a_bridge_publishes_its_own_topics_and_reads_only_its_commands(auth_on):
    client = TestClient(backend.app)
    assert _acl(client, "rover-a", "roverhub/site-a/rover-a/telemetry", ACC_WRITE) == 200
    assert _acl(client, "rover-a", "roverhub/site-a/rover-a/commands", ACC_SUBSCRIBE) == 200
    assert _acl(client, "rover-a", "roverhub/site-a/rover-a/commands", ACC_WRITE) == 403
    assert _acl(client, "rover-a", "roverhub/site-b/rover-b/commands", ACC_SUBSCRIBE) == 403
    assert _acl(client, "rover-a", "roverhub/admin/bridge-a/register", ACC_WRITE) == 200


def test_a_browser_reads_only_its_sites_and_never_publishes(auth_on):
    user = _user(auth_on, "ana", {"site-a": "operator"})
    client = TestClient(backend.app)
    uid = user.user_id
    assert _acl(client, uid, "roverhub/+/+/telemetry", ACC_SUBSCRIBE) == 200
    assert _acl(client, uid, "roverhub/site-a/rover-a/telemetry", ACC_READ) == 200
    assert _acl(client, uid, "roverhub/site-b/rover-b/telemetry", ACC_READ) == 403
    assert _acl(client, uid, "roverhub/admin/+/register", ACC_SUBSCRIBE) == 200
    assert _acl(client, uid, "roverhub/site-a/rover-a/commands", ACC_WRITE) == 403
    assert _acl(client, uid, "roverhub/site-a/rover-a/commands", ACC_READ) == 403


# --- the role table matches the console's ---------------------------------

_ROLES_TS = Path(__file__).resolve().parents[2] / "roverhub-frontend" / "src" / "org" / "roles.ts"


@pytest.mark.skipif(not _ROLES_TS.exists(), reason="roverhub-frontend is not checked out beside this repo")
def test_capabilities_match_the_frontend_role_table():
    source = _ROLES_TS.read_text(encoding="utf-8")
    pairs = re.findall(r"cap\('(\w+)',\s*\[([^\]]*)\]\)", source)
    frontend = {cap: frozenset(re.findall(r"'(\w+)'", roles)) for cap, roles in pairs}
    assert frontend == CAPABILITIES


def test_a_user_added_by_the_cli_while_the_server_runs_is_seen_and_kept(tmp_path):
    path = str(tmp_path / "users.json")
    server = UserStore(path)
    server.create(username="first", display_name="", password=PASSWORD)
    UserStore(path).create(username="from-cli", display_name="", password=PASSWORD)  # another process
    assert server.check_credentials("from-cli", PASSWORD) is not None
    server.create(username="later", display_name="", password=PASSWORD)
    assert {u.username for u in UserStore(path).list()} == {"first", "from-cli", "later"}
