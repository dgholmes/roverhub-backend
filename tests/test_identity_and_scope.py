"""Tracker 3-06 and 3-07: a robot's identity comes from its credential and its
topic, never a message body; lists show only the caller's sites; the audit log
records who changed what and shows each reader only their rows."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import api.app as backend
from auth.deps import BridgeBindings, Principal, audit_scope
from auth.mqtt_hooks import ACC_READ, ACC_SUBSCRIBE, ACC_WRITE
from auth.tokens import TokenIssuer
from auth.users import User, UserStore
from control.commands import CommandStore
from history.records import AuditRecord, IncidentRecord
from history.store import HistoryStore, _migrations, _statements
from realtime.mqtt_client import BackendMqttClient
from robots.registry import RobotRegistry
from shared.schemas.commands import Command

PASSWORD = "correct horse"
T0 = 1_762_840_800.0


# -- the MQTT subscriber: topic, not body ----------------------------------------------


class _Paho:
    """Enough of paho for BackendMqttClient to be built and handed messages."""

    def subscribe(self, topic, qos=0):
        pass


def _subscriber(registry, **hooks):
    return BackendMqttClient("mqtt://localhost:1883", registry, _Paho, **hooks)


def _registry(*robots):
    registry = RobotRegistry()
    for robot_id, site_id in robots:
        registry.upsert_from_registration(robot_id, site_id, "quad", bridge_id=f"bridge-{robot_id}")
        registry.mark_offline(robot_id)
    return registry


def _send(client, topic, payload):
    client.handle_message(topic, json.dumps(payload).encode())


def test_telemetry_whose_body_names_another_robot_is_attributed_to_neither():
    registry = _registry(("rover-a", "site-a"), ("rover-b", "site-a"))
    observed = []
    client = _subscriber(registry, on_observed=lambda *args: observed.append(args))

    _send(client, "roverhub/site-a/rover-a/telemetry", {"robot_id": "rover-b", "battery_percent": 50})
    _send(client, "roverhub/site-a/rover-a/heartbeat", {"robot_id": "rover-b"})

    assert registry.get("rover-a").online is False and registry.get("rover-b").online is False
    assert observed == []


def test_state_whose_body_names_another_robot_changes_nothing():
    registry = _registry(("rover-a", "site-a"), ("rover-b", "site-a"))
    states = []
    client = _subscriber(registry, on_state=lambda *args: states.append(args))

    _send(client, "roverhub/site-a/rover-a/state",
          {"robot_id": "rover-b", "abstract_state": "E_STOP", "sdk_state": "passive"})

    assert registry.get("rover-a").abstract_state is None
    assert registry.get("rover-b").abstract_state is None
    assert states == []


def test_a_body_without_a_robot_is_attributed_to_the_topics_robot():
    registry = _registry(("rover-a", "site-a"))
    client = _subscriber(registry)
    _send(client, "roverhub/site-a/rover-a/state", {"abstract_state": "STAND", "sdk_state": "balance_stand"})
    _send(client, "roverhub/site-a/rover-a/heartbeat", {})
    record = registry.get("rover-a")
    assert record.abstract_state == "STAND" and record.online is True


def test_a_message_under_another_sites_prefix_is_dropped():
    registry = _registry(("rover-a", "site-a"))
    observed = []
    client = _subscriber(registry, on_observed=lambda *args: observed.append(args))

    _send(client, "roverhub/site-b/rover-a/heartbeat", {"robot_id": "rover-a"})
    _send(client, "roverhub/site-b/rover-a/incidents", {"incident_id": "rover-a-1"})
    _send(client, "roverhub/site-b/rover-a/perception/status", {"robot_id": "rover-a"})

    assert registry.get("rover-a").online is False
    assert observed == []


def test_an_incident_naming_another_robot_is_filed_under_the_topic():
    registry = _registry(("rover-a", "site-a"))
    observed = []
    client = _subscriber(registry, on_observed=lambda *args: observed.append(args))
    _send(client, "roverhub/site-a/rover-a/incidents", {"incident_id": "x", "robot_id": "rover-b"})
    assert [(kind, site, robot) for kind, site, robot, _ in observed] == [("incident", "site-a", "rover-a")]


def test_an_ack_whose_body_names_another_robot_is_not_applied():
    registry = _registry(("rover-a", "site-a"), ("rover-b", "site-a"))
    acks = []
    client = _subscriber(registry, on_ack=acks.append)
    _send(client, "roverhub/site-a/rover-a/ack", {
        "command_id": "c-1", "robot_id": "rover-b", "type": "ESTOP", "label": "E-Stop",
        "current_stage": "execution_completed", "initiated_by": "x"})
    assert acks == []


def test_a_registration_takes_its_bridge_from_the_topic_and_can_be_refused():
    registry = RobotRegistry()
    asked = []

    def check(bridge_id, robot_id, known):
        asked.append((bridge_id, robot_id, known))
        return robot_id != "rover-b"

    client = _subscriber(registry, registration_check=check)
    _send(client, "roverhub/admin/bridge-pi/register",
          {"bridge_id": "bridge-other", "robot_id": "rover-a", "site_id": "site-a", "robot_type": "quad"})
    _send(client, "roverhub/admin/bridge-pi/register",
          {"bridge_id": "bridge-pi", "robot_id": "rover-b", "site_id": "site-a", "robot_type": "quad"})

    assert registry.get("rover-a").bridge_id == "bridge-pi"
    assert registry.get("rover-b") is None
    assert asked == [("bridge-pi", "rover-a", None), ("bridge-pi", "rover-b", None)]


# -- acks cannot rewrite what was sent -------------------------------------------------


def _command(**extra):
    base = {"command_id": "c-1", "robot_id": "rover-a", "type": "START_MISSION", "label": "Start",
            "params": {"run_id": "run-a"}, "initiated_by": "user-ana"}
    return Command(**{**base, **extra})


def test_an_ack_for_a_command_sent_to_another_robot_is_refused():
    store = CommandStore()
    store.add(_command())
    assert store.apply_ack(_command(robot_id="rover-b", current_stage="execution_failed")) is None
    assert store.get("c-1").current_stage == "requested"


def test_an_ack_keeps_what_the_server_sent():
    store = CommandStore()
    store.add(_command())
    acked = store.apply_ack(_command(current_stage="execution_failed", initiated_by="mallory",
                                     params={"run_id": "someone-elses-run"}, type="ESTOP"))
    assert (acked.initiated_by, acked.params, acked.type) == ("user-ana", {"run_id": "run-a"}, "START_MISSION")
    assert acked.current_stage == "execution_failed"
    assert store.get("c-1") == acked


def test_an_ack_for_a_command_this_process_never_sent_is_stored():
    store = CommandStore()
    acked = store.apply_ack(_command(current_stage="execution_completed"))
    assert acked is not None and store.get("c-1").current_stage == "execution_completed"


# -- the real app, signed in -------------------------------------------------------------


class _Published:
    rc = 0
    mid = 1


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(backend.mqtt_client, "connect", lambda: None)
    monkeypatch.setattr(backend.mqtt_client, "disconnect", lambda: None)
    monkeypatch.setattr(backend.mqtt_client._client, "publish", lambda topic, payload, qos=0: _Published())
    backend.registry._robots.clear()
    backend.registry.upsert_from_registration("rover-a", "site-a", "quad", bridge_id="bridge-a")
    backend.registry.upsert_from_registration("rover-b", "site-b", "quad", bridge_id="bridge-b")
    with TestClient(backend.app) as client:
        yield client
    backend.registry._robots.clear()


@pytest.fixture
def auth_on(tmp_path, monkeypatch, app):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    monkeypatch.setattr(auth, "device_keys", {"rover-a": "key-a", "rover-b": "key-b"})
    monkeypatch.setattr(auth, "broker_service_password", "service-pw")
    monkeypatch.setattr(auth, "bridges", BridgeBindings())
    users = {
        "ana": dict(site_roles={"site-a": "supervisor"}),
        "vic": dict(site_roles={"site-a": "viewer"}),
        "bob": dict(site_roles={"site-b": "supervisor"}),
        "own": dict(site_roles={"*": "supervisor"}, org_role="owner"),
        "aud": dict(site_roles={"*": "viewer"}, audit_access=True),
        "eng": dict(site_roles={"*": "engineer"}),
    }
    ids = {name: auth.users.create(username=name, display_name=name, password=PASSWORD, **spec).user_id
           for name, spec in users.items()}

    def headers(name):
        token = app.post("/auth/login", json={"username": name, "password": PASSWORD}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    headers.ids = ids
    return headers


def _acl(client, username, topic, acc):
    return client.post("/mqtt/acl", json={"username": username, "clientid": "c", "topic": topic,
                                          "acc": acc}).status_code


def test_a_device_binds_its_registration_topic_and_no_other_device_may_write_it(app, auth_on):
    assert _acl(app, "rover-a", "roverhub/admin/bridge-a/register", ACC_WRITE) == 200
    assert _acl(app, "rover-b", "roverhub/admin/bridge-a/register", ACC_WRITE) == 403
    assert _acl(app, "rover-a", "roverhub/admin/bridge-a/register", ACC_WRITE) == 200
    # A bridge never reads registrations, and may not subscribe to everyone's.
    assert _acl(app, "rover-a", "roverhub/admin/+/register", ACC_SUBSCRIBE) == 403
    assert _acl(app, "rover-a", "roverhub/admin/bridge-b/register", ACC_READ) == 403
    assert _acl(app, "rover-a", "roverhub/admin/+/register", ACC_WRITE) == 403


def test_a_registration_naming_another_robot_is_not_believed(app, auth_on):
    assert _acl(app, "rover-a", "roverhub/admin/evil/register", ACC_WRITE) == 200
    backend.mqtt_client.handle_message("roverhub/admin/evil/register", json.dumps(
        {"bridge_id": "evil", "robot_id": "rover-b", "site_id": "site-a", "robot_type": "quad"}).encode())
    record = backend.registry.get("rover-b")
    assert (record.site_id, record.bridge_id) == ("site-b", "bridge-b")


def test_an_unbound_registration_is_refused_only_when_a_bound_bridge_speaks_for_the_robot(auth_on):
    auth = backend.auth
    assert auth.registration_allowed("bridge-x", "rover-b", "bridge-b") is True    # nothing bound yet
    auth.bridges.bind("bridge-b", "rover-b")
    assert auth.registration_allowed("bridge-b", "rover-b", "bridge-b") is True
    assert auth.registration_allowed("bridge-x", "rover-b", "bridge-b") is False   # a stranger
    auth.bridges.bind("bridge-x", "rover-a")
    assert auth.registration_allowed("bridge-x", "rover-b", None) is False         # bound to rover-a
    assert auth.registration_allowed("bridge-x", "rover-a", None) is True


def test_registrations_are_not_checked_with_sign_in_off(monkeypatch):
    auth = backend.auth
    monkeypatch.setattr(auth, "bridges", BridgeBindings())
    auth.bridges.bind("bridge-x", "rover-a")
    assert auth.enabled is False
    assert auth.registration_allowed("bridge-x", "rover-b", None) is True


def test_a_device_writes_its_own_topics_only_under_its_registered_site(app, auth_on):
    assert _acl(app, "rover-a", "roverhub/site-a/rover-a/telemetry", ACC_WRITE) == 200
    assert _acl(app, "rover-a", "roverhub/site-a/rover-a/scan", ACC_WRITE) == 200
    assert _acl(app, "rover-a", "roverhub/site-b/rover-a/telemetry", ACC_WRITE) == 403
    assert _acl(app, "rover-a", "roverhub/site-a/rover-b/telemetry", ACC_WRITE) == 403
    assert _acl(app, "rover-a", "roverhub/site-a/+/telemetry", ACC_SUBSCRIBE) == 403
    assert _acl(app, "rover-a", "roverhub/site-a/rover-a/#", ACC_SUBSCRIBE) == 403
    # Subscriptions are not pinned: a refusal cached by the broker would cut
    # a bridge off from its commands after a site change.
    assert _acl(app, "rover-a", "roverhub/site-b/rover-a/commands", ACC_SUBSCRIBE) == 200
    backend.registry.forget("rover-a")
    assert _acl(app, "rover-a", "roverhub/site-z/rover-a/telemetry", ACC_WRITE) == 200  # site not known


def test_a_browser_may_read_the_scan_of_its_own_sites_only(app, auth_on):
    ana = auth_on.ids["ana"]
    assert _acl(app, ana, "roverhub/site-a/rover-a/scan", ACC_READ) == 200
    assert _acl(app, ana, "roverhub/site-b/rover-b/scan", ACC_READ) == 403


# -- lists show the caller's sites ------------------------------------------------------


def test_robots_are_listed_for_the_callers_sites(app, auth_on):
    ids = lambda name: sorted(r["robot_id"] for r in app.get("/robots", headers=auth_on(name)).json())  # noqa: E731
    assert ids("ana") == ["rover-a"]
    assert ids("bob") == ["rover-b"]
    assert ids("own") == ["rover-a", "rover-b"]


def _history_sample(robot_id, site_id, at=T0):
    from history.records import SampleRecord

    return SampleRecord(robot_id=robot_id, site_id=site_id, at=at, battery_pct=80.0, x_m=0.0, y_m=0.0,
                        heading_deg=0.0, position_quality="localized", speed_mps=0.0, abstract_state="STAND",
                        sdk_state="balance_stand", posture="upright", control_owner=None, active_run_id=None,
                        link="live", reason="interval")


def test_the_history_of_a_robot_the_registry_forgot_is_checked_at_its_last_site(app, auth_on):
    if not backend.history.available:
        pytest.skip("history is not available in this run")
    ghost = f"ghost-{uuid.uuid4().hex[:6]}"
    backend.history.store.write([_history_sample(ghost, "site-b")])
    window = {"from": "2025-11-10T00:00:00Z", "to": "2025-11-12T00:00:00Z"}
    assert backend.registry.get(ghost) is None
    # ana holds site-a only: before, a robot unknown to the registry had no
    # site, so anyone who could watch anywhere could read its history.
    assert app.get(f"/robots/{ghost}/history", params=window, headers=auth_on("ana")).status_code == 403
    response = app.get(f"/robots/{ghost}/history", params=window, headers=auth_on("bob"))
    assert response.status_code == 200 and len(response.json()["samples"]) == 1
    # Nothing anywhere: no site to check, and the handler says it is unknown.
    assert app.get("/robots/nobody-at-all/history", params=window, headers=auth_on("ana")).status_code == 404


def test_a_robots_history_leaves_out_the_sites_the_reader_cannot_see(app, auth_on):
    if not backend.history.available:
        pytest.skip("history is not available in this run")
    # rover-b is at site-b now, but some of its samples are from site-a.
    backend.history.store.write([_history_sample("rover-b", "site-a", T0), _history_sample("rover-b", "site-b", T0 + 5)])
    window = {"from": "2025-11-10T00:00:00Z", "to": "2025-11-12T00:00:00Z"}
    bob = app.get("/robots/rover-b/history", params=window, headers=auth_on("bob")).json()["samples"]
    own = app.get("/robots/rover-b/history", params=window, headers=auth_on("own")).json()["samples"]
    assert len(own) == len(bob) + 1


def test_a_speak_clip_is_read_at_its_robots_site(app, auth_on):
    import os

    clips_dir = os.environ["AUDIO_CLIPS_DIR"]
    os.makedirs(clips_dir, exist_ok=True)
    clip_id = f"rover-b-{uuid.uuid4()}.pcm"
    with open(os.path.join(clips_dir, clip_id), "wb") as f:
        f.write(bytes(2))
    assert app.get(f"/audio-clips/{clip_id}", headers=auth_on("ana")).status_code == 403   # site-a only
    assert app.get(f"/audio-clips/{clip_id}", headers=auth_on("bob")).status_code == 200
    assert app.get(f"/audio-clips/{clip_id}", headers=auth_on("own")).status_code == 200
    # A device key may still fetch it -- a mission's Speak step keeps its clip
    # when the mission runs on another robot -- but only with a key.
    assert app.get(f"/audio-clips/{clip_id}", headers={"Authorization": "Bearer key-a"}).status_code == 200
    app.cookies.clear()  # the last sign-in's session cookie would authorise a GET
    assert app.get(f"/audio-clips/{clip_id}").status_code == 401


def test_a_clips_robot_is_read_from_its_id():
    from control.media import robot_of_clip

    uid = uuid.uuid4()
    assert robot_of_clip(f"rover-b-{uid}.pcm") == "rover-b"
    assert robot_of_clip(f"my_robot-2-{uid}.wav") == "my_robot-2"
    assert robot_of_clip("rover-b.pcm") is None
    assert robot_of_clip(f"../rover-b-{uid}.pcm") is None


def _mission(mission_id, site_id, version=1, creator="someone-else"):
    return {"mission_id": mission_id, "name": "Patrol", "type": "patrol", "kind": "route", "version": version,
            "creator": creator, "updated_at": "2026-11-11T00:00:00Z", "map_id": "map", "site_id": site_id}


def _task(task_id, mission_id, site_id, robot_id, version=1):
    return {"task_id": task_id, "mission_id": mission_id, "site_id": site_id, "robot_id": robot_id,
            "created_by": "someone-else", "created_at": "2026-11-11T00:00:00Z",
            "updated_at": "2026-11-11T00:00:00Z", "version": version}


@pytest.fixture
def seeded(app, auth_on):
    """A mission and a task at each site, made by the owner; removed afterwards."""
    tag = uuid.uuid4().hex[:8]
    own = auth_on("own")
    made = {}
    for site in ("site-a", "site-b"):
        mission_id, task_id = f"m-{site}-{tag}", f"t-{site}-{tag}"
        robot = "rover-a" if site == "site-a" else "rover-b"
        assert app.put(f"/missions/{mission_id}", json=_mission(mission_id, site), headers=own).status_code == 200
        assert app.put(f"/tasks/{task_id}", json=_task(task_id, mission_id, site, robot), headers=own).status_code == 200
        made[site] = (mission_id, task_id)
    yield made
    for mission_id, task_id in made.values():
        app.delete(f"/tasks/{task_id}", headers=own)
        app.delete(f"/missions/{mission_id}", headers=own)


def test_missions_and_tasks_are_listed_for_the_callers_sites(app, auth_on, seeded):
    ana, bob, own = auth_on("ana"), auth_on("bob"), auth_on("own")
    mine = {m for m, _ in seeded.values()}
    listed = lambda h: {m["mission_id"] for m in app.get("/missions", headers=h).json()["items"]} & mine  # noqa: E731
    assert listed(ana) == {seeded["site-a"][0]}
    assert listed(bob) == {seeded["site-b"][0]}
    assert listed(own) == mine
    tasks = {t for _, t in seeded.values()}
    listed_tasks = lambda h: {t["task_id"] for t in app.get("/tasks", headers=h).json()["items"]} & tasks  # noqa: E731
    assert listed_tasks(ana) == {seeded["site-a"][1]}
    # Asking for another site by name shows nothing rather than leaking it.
    assert app.get("/missions", params={"site_id": "site-b"}, headers=ana).json()["items"] == []
    # And one by id is refused at the guard (its site is resolved from the store).
    assert app.get(f"/missions/{seeded['site-b'][0]}", headers=ana).status_code == 403


def test_a_write_naming_another_site_in_its_body_is_refused(app, auth_on, seeded):
    ana = auth_on("ana")
    mission_a, task_a = seeded["site-a"]
    # A create at a site where ana does not plan.
    fresh = f"m-new-{uuid.uuid4().hex[:6]}"
    assert app.put(f"/missions/{fresh}", json=_mission(fresh, "site-b"), headers=ana).status_code == 403
    # Moving her own site's mission to another site.
    assert app.put(f"/missions/{mission_a}", json=_mission(mission_a, "site-b"), headers=ana).status_code == 403
    assert app.put(f"/tasks/{task_a}", json=_task(task_a, mission_a, "site-b", "rover-b"),
                   headers=ana).status_code == 403
    # Her own site is fine, and the creator is the server's to say.
    saved = app.put(f"/missions/{mission_a}", json=_mission(mission_a, "site-a"), headers=ana).json()
    assert saved["creator"] == auth_on.ids["own"] and saved["version"] == 2


def _with_principal(router, user: User):
    app = FastAPI()

    async def as_user(request: Request):
        request.state.principal = Principal(kind="user", id=user.user_id, user=user)

    from fastapi import Depends
    app.include_router(router, dependencies=[Depends(as_user)])
    return TestClient(app)


def _user(site_roles):
    return User(user_id="user-x", username="xx", display_name="x", site_roles=site_roles, password_hash="-")


def test_runs_are_listed_and_counted_for_the_callers_sites(tmp_path):
    from runs.router import build_runs_router
    from shared.schemas.runs import MissionRun

    router = build_runs_router(str(tmp_path))
    for site, robot in (("site-a", "rover-a"), ("site-b", "rover-b")):
        router.store.put(MissionRun.model_validate({
            "run_id": f"20261111-060000-m-1-{robot}", "mission_id": "m-1",
            "mission": _mission("m-1", site), "robot_id": robot, "site_id": site,
            "started_at": "2026-11-11T06:00:00Z", "started_by": "x"}))
    body = _with_principal(router, _user({"site-a": "viewer"})).get("/runs").json()
    assert [r["robot_id"] for r in body["items"]] == ["rover-a"] and body["total"] == 1


def test_snapshots_are_listed_for_the_callers_sites_including_ones_without_a_site(tmp_path):
    from control.snapshots import build_snapshot_router

    sites = {"rover-a": "site-a", "rover-b": "site-b"}
    router = build_snapshot_router(snapshots_dir=str(tmp_path), site_lookup=sites.get)
    for robot, site in (("rover-a", "site-a"), ("rover-b", "site-b"), ("rover-b", None)):
        router.index.add({"snapshot_id": f"{robot}-{uuid.uuid4()}.jpg", "robot_id": robot, "site_id": site,
                          "received_at": "2026-11-11T06:00:00+00:00"})
    items = _with_principal(router, _user({"site-a": "viewer"})).get("/snapshots").json()["items"]
    assert [i["robot_id"] for i in items] == ["rover-a"]
    items = _with_principal(router, _user({"site-b": "viewer"})).get("/snapshots").json()["items"]
    assert sorted(i["robot_id"] for i in items) == ["rover-b", "rover-b"]   # the site-less one by its robot


def test_maps_are_listed_for_the_callers_sites_and_uploaded_only_where_they_configure(tmp_path):
    from maps.router import build_maps_router
    from tests.map_bundles import bundle_parts, make_bundle

    audited = []
    router = build_maps_router(str(tmp_path / "maps"), audit=lambda *a, **k: audited.append((a, k)))

    def upload(client, map_id, site_id):
        parts = bundle_parts(make_bundle(tmp_path / f"b-{map_id}-{site_id}", map_id=map_id, site_id=site_id))
        return client.post("/maps", files={n: (n.rsplit("/", 1)[-1], d, "application/octet-stream")
                                           for n, d in parts.items()})

    eng = _with_principal(router, _user({"site-a": "engineer", "site-b": "engineer"}))
    assert upload(eng, "map-a", "site-a").status_code == 201
    assert upload(eng, "map-b", "site-b").status_code == 201
    only_a = _with_principal(router, _user({"site-a": "engineer"}))
    assert [m["map_id"] for m in only_a.get("/maps").json()["items"]] == ["map-a"]
    assert upload(only_a, "map-c", "site-b").status_code == 403
    # Replacing another site's map by re-using its id under her own site.
    assert upload(only_a, "map-b", "site-a").status_code == 403
    assert [(a[0], a[1], k["site_id"]) for a, k in audited] == [
        ("map.upload", "map-a", "site-a"), ("map.upload", "map-b", "site-b")]


def test_incidents_are_listed_for_the_callers_sites(history_store):
    def incident(iid, robot, site):
        return IncidentRecord(payload={"incident_id": iid, "label": "x", "severity": "info",
                                       "opened_at": "2026-11-11T06:00:00Z"},
                              robot_id=robot, site_id=site, received_at=T0)
    history_store.write([incident("i-a", "rover-a", "site-a"), incident("i-b", "rover-b", "site-b")])
    ids = lambda sites: [i["incident_id"] for i in history_store.list_incidents(sites=sites)[0]]  # noqa: E731
    assert ids(None) == ["i-b", "i-a"] or sorted(ids(None)) == ["i-a", "i-b"]
    assert ids(frozenset({"site-a"})) == ["i-a"]
    assert ids(frozenset()) == []


# -- the audit log ------------------------------------------------------------------------


def _audit(app, headers, **params):
    assert backend.history.flush(timeout_s=5)
    response = app.get("/audit", headers=headers, params={"limit": 500, **params})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_mission_and_task_changes_are_audited_to_the_signed_in_user(app, auth_on, seeded):
    own_id = auth_on.ids["own"]
    own = auth_on("own")
    mission_a, task_a = seeded["site-a"]
    app.put(f"/missions/{mission_a}", json=_mission(mission_a, "site-a"), headers=own)
    rows = _audit(app, own, target=mission_a)
    assert [(r["action"], r["actor"], r["site_id"]) for r in reversed(rows)] == [
        ("mission.create", own_id, "site-a"), ("mission.update", own_id, "site-a")]
    task_rows = _audit(app, own, target=task_a)
    assert [(r["action"], r["actor"], r["site_id"], r["robot_id"]) for r in task_rows] == [
        ("task.create", own_id, "site-a", "rover-a")]
    # Filterable by robot, and by a family of actions.
    by_robot = _audit(app, own, robot_id="rover-a", action="task.*")
    assert task_a in {r["target"] for r in by_robot}
    assert all(r["robot_id"] == "rover-a" and r["action"].startswith("task.") for r in by_robot)


def test_each_reader_sees_only_their_sites_and_org_rows_only_if_they_audit_the_org(app, auth_on, seeded):
    ana, bob, aud, vic = auth_on("ana"), auth_on("bob"), auth_on("aud"), auth_on("vic")
    targets = {m for m, _ in seeded.values()}
    seen = lambda h: {r["target"] for r in _audit(app, h, action="mission.create")} & targets  # noqa: E731
    assert seen(ana) == {seeded["site-a"][0]}
    assert seen(bob) == {seeded["site-b"][0]}
    assert seen(aud) == targets                          # audit access with a role at every site
    assert app.get("/audit", headers=vic).status_code == 403  # a viewer without audit access
    # Sign-ins have no site: the auditor sees them, a site supervisor does not.
    assert _audit(app, ana, action="auth.login") == []
    assert {r["detail"]["username"] for r in _audit(app, aud, action="auth.login")} >= {"ana", "bob", "own"}
    # Asking for another site by name gives nothing.
    assert {r["target"] for r in _audit(app, ana, site_id="site-b")} == set()


def test_user_and_role_changes_and_robot_decisions_are_audited(app, auth_on):
    own_id = auth_on.ids["own"]
    own = auth_on("own")
    made = app.post("/users", headers=own, json={"username": "newbie", "password": PASSWORD,
                                                  "site_roles": {"site-a": "viewer"}})
    assert made.status_code == 201
    uid = made.json()["user_id"]
    assert app.patch(f"/users/{uid}", headers=own, json={"site_roles": {"site-a": "operator"}}).status_code == 200
    assert app.post(f"/users/{uid}/sign-out", headers=own).status_code == 204
    assert app.delete(f"/users/{uid}", headers=own).status_code == 204
    assert app.post("/robots/rover-a/reject", headers=auth_on("eng")).status_code == 200
    rows = _audit(app, own, target=uid)
    assert [(r["action"], r["actor"], r["site_id"]) for r in reversed(rows)] == [
        ("user.create", own_id, None), ("user.update", own_id, None), ("user.sign_out", own_id, None),
        ("user.delete", own_id, None)]
    update = next(r for r in rows if r["action"] == "user.update")
    assert update["detail"]["changes"] == {"site_roles": {"from": {"site-a": "viewer"}, "to": {"site-a": "operator"}}}
    robot = _audit(app, own, target="rover-a", action="robot.*")
    assert [(r["action"], r["site_id"], r["robot_id"]) for r in robot][:1] == [("robot.reject", "site-a", "rover-a")]
    failed = app.post("/auth/login", json={"username": "own", "password": "wrong password"})
    assert failed.status_code == 401
    refused = [r for r in _audit(app, own, action="auth.login") if r["outcome"] == "refused"]
    assert refused and refused[0]["detail"]["username"] == "own"


def test_audit_scope_follows_roles_and_the_audit_flag():
    def scope(site_roles, org_role="member", audit_access=False):
        user = User(user_id="u", username="uu", display_name="u", site_roles=site_roles, org_role=org_role,
                    audit_access=audit_access, password_hash="-")
        return audit_scope(Principal(kind="user", id="u", user=user))

    s = scope({"site-a": "supervisor", "site-b": "operator"})
    assert (s.allows_site("site-a"), s.allows_site("site-b"), s.allows_site(None)) == (True, False, False)
    s = scope({"*": "engineer", "site-b": "viewer"})
    assert (s.allows_site("site-x"), s.allows_site("site-b")) == (True, False)
    s = scope({"site-b": "viewer"}, audit_access=True)
    assert (s.allows_site("site-b"), s.allows_site("site-a"), s.allows_site(None)) == (True, False, True)
    s = scope({}, org_role="admin")
    assert (s.allows_site("site-a"), s.allows_site(None)) == (False, True)
    assert audit_scope(Principal(kind="dev", id="operator-dev")).allows_site("anything")


def _rows(store, **kw):
    return [r["target"] for r in store.list_audit(**kw)[0]]


def test_the_store_filters_audit_rows_by_site_robot_family_and_scope(history_store):
    from auth.deps import AuditScope

    history_store.write([
        AuditRecord(T0 + 1, "ana", "mission.create", "m-a", "ok", {}, "site-a"),
        AuditRecord(T0 + 2, "bob", "task.create", "t-b", "ok", {}, "site-b", "rover-b"),
        AuditRecord(T0 + 3, "own", "user.create", "u-1", "ok", {}, None),
        AuditRecord(T0 + 4, "ana", "command.publish", "rover-a", "sent", {}, "site-a", "rover-a"),
    ])
    assert _rows(history_store, site_id="site-a") == ["rover-a", "m-a"]
    assert _rows(history_store, robot_id="rover-b") == ["t-b"]
    assert _rows(history_store, action="mission.*") == ["m-a"]
    only_a = AuditScope(all_sites=False, sites=frozenset({"site-a"}), org_rows=False)
    assert _rows(history_store, scope=only_a) == ["rover-a", "m-a"]
    org = AuditScope(all_sites=False, sites=frozenset(), org_rows=True)
    assert _rows(history_store, scope=org) == ["u-1"]
    all_but_b = AuditScope(all_sites=True, sites=frozenset(), org_rows=False, excluded=frozenset({"site-b"}))
    assert _rows(history_store, scope=all_but_b) == ["rover-a", "m-a"]
    nothing = AuditScope(all_sites=False, sites=frozenset(), org_rows=False)
    assert _rows(history_store, scope=nothing) == []


def test_migration_2_backfills_the_robot_of_command_and_incident_rows(history_db):
    store = HistoryStore(history_db)
    (first, *_rest) = _migrations()
    with store.transaction(write=True) as conn:
        store._exec(conn, history_db.ddl(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at {{REAL}} NOT NULL)"))
        for statement in _statements(first[2].read_text(encoding="utf-8")):
            store._exec(conn, history_db.ddl(statement))
        store._exec(conn, "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, 'initial', 0)")
        j = history_db.json_param()
        store._exec(conn, "INSERT INTO audit_log (at, actor, action, target, outcome, site_id, detail) "
                          f"VALUES (?, 'ana', 'command.publish', 'rover-a', 'sent', 'site-a', {j})", (T0, "{}"))
        store._exec(conn, "INSERT INTO incidents (incident_id, robot_id, site_id, label, severity, opened_at, "
                          f"payload, evidence_snapshot_ids, received_at, last_seen, state) VALUES "
                          f"('i-1', 'rover-b', 'site-b', 'x', 'info', ?, {j}, {j}, ?, ?, 'open')",
                    (T0, "{}", "[]", T0, T0))
        store._exec(conn, "INSERT INTO audit_log (at, actor, action, target, outcome, site_id, detail) "
                          f"VALUES (?, 'ana', 'incident.acknowledge', 'i-1', 'ok', 'site-b', {j})", (T0 + 1, "{}"))
    store.migrate()
    rows = {r["target"]: r["robot_id"] for r in store.list_audit()[0]}
    assert rows == {"rover-a": "rover-a", "i-1": "rover-b"}
    store.close()
