"""Site setup (schema 0.0.23): surveying from the console, the lidar mount and
calibration mode, and the moving-object search re-run.

- the five commands through POST /robots/{id}/commands: who may send each,
  their params, the bridge-version gate, the map they name, attribution;
- the broker's rules for survey/status, survey/map and sensors/lidar_mount;
- a robot's device key uploading the draft of its own survey (POST
  /map-surveys, PUT planning, PUT moved, polling the job, reading the map back),
  at its own registered site only, and the audit rows that name it;
- PUT moved in the survey's frame, with its points (what FIND_MOVING sends).
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

np = pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.deps import BridgeBindings, Principal  # noqa: E402
from auth.mqtt_hooks import ACC_READ, ACC_SUBSCRIBE, ACC_WRITE  # noqa: E402
from auth.policy import ROUTES, command_capabilities  # noqa: E402
from auth.tokens import TokenIssuer  # noqa: E402
from auth.users import UserStore  # noqa: E402
from control.router import SURVEY_SCHEMA_VERSION  # noqa: E402
from maps import cloud_edit as ce  # noqa: E402
from maps.router import build_maps_router  # noqa: E402
from shared.schemas.commands import WIRED_COMMAND_TYPES, command_ttl_s  # noqa: E402
from shared.schemas.version import SCHEMA_VERSION  # noqa: E402
from tests.map_bundles import make_bundle  # noqa: E402
from tests.test_maps_editor import MAP, MOVED_POINTS, make_room_bundle, moved_doc, pgm, planning_image  # noqa: E402
from tests.test_maps_pipeline import room_cloud, room_track, track_bytes, write_pcd  # noqa: E402

PASSWORD = "correct horse"
SITE, OTHER_SITE = "site-venue", "site-other"
SITE_SETUP = ("START_SURVEY", "STOP_SURVEY", "FIND_MOVING", "SET_LIDAR_MOUNT", "SET_CALIBRATION_MODE")
MOUNT = {"mount": {"x_m": 0.234, "y_m": 0.0, "z_m": 0.12, "pitch_deg": 2.0}}


def _params(type_, map_id=None):
    return {
        "START_SURVEY": {"survey_id": "s-1", "name": "Office"},
        "STOP_SURVEY": {"survey_id": "s-1", "save": True},
        "FIND_MOVING": {"survey_id": "s-1", "map_id": map_id or "map-x", "floor_index": 0, "sensitivity": 0.7},
        "SET_LIDAR_MOUNT": MOUNT,
        "SET_CALIBRATION_MODE": {"mode": "auto"},
    }[type_]


# --- fixtures: the real app, its registry and map store ---------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    """rover-a at site-venue on 0.0.23, rover-b at site-other, rover-old at
    site-venue on 0.0.22; a map at each site. Commands are captured, not sent."""
    backend.registry._robots.clear()
    backend.registry.upsert_from_registration("rover-a", SITE, "quad", schema_version=SCHEMA_VERSION)
    backend.registry.upsert_from_registration("rover-b", OTHER_SITE, "quad", schema_version=SCHEMA_VERSION)
    backend.registry.upsert_from_registration("rover-old", SITE, "quad", schema_version="0.0.22")
    sent = []
    monkeypatch.setattr(backend.mqtt_client, "publish_command", lambda robot_id, command: sent.append(command))
    store = backend.maps_router.store
    here = "map-here-%s" % uuid.uuid4().hex[:8]
    there = "map-there-%s" % uuid.uuid4().hex[:8]
    store.save_bundle(make_room_bundle(tmp_path / "here", map_id=here))
    store.save_bundle(make_bundle(tmp_path / "there", map_id=there, site_id=OTHER_SITE))
    yield {"here": here, "there": there, "sent": sent}
    store.delete(here)
    store.delete(there)
    backend.registry._robots.clear()


@pytest.fixture
def auth_on(world, tmp_path, monkeypatch):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    monkeypatch.setattr(auth, "device_keys", {"rover-a": "key-a", "rover-b": "key-b", "rover-gone": "key-gone"})
    monkeypatch.setattr(auth, "broker_service_password", "service-pw")
    monkeypatch.setattr(auth, "bridges", BridgeBindings())
    for name, roles in (("eve", {SITE: "engineer"}), ("sue", {SITE: "supervisor"}), ("ann", {SITE: "operator"}),
                        ("tom", {SITE: "technician"}), ("vic", {SITE: "viewer"}),
                        ("far", {OTHER_SITE: "engineer"})):
        auth.users.create(username=name, display_name=name, password=PASSWORD, org_role="member", site_roles=roles)
    return auth


def _bearer(client, username):
    token = client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": "Bearer " + token}


def _command(client, type_, params, robot="rover-a", headers=None):
    return client.post("/robots/%s/commands" % robot, headers=headers or {},
                       json={"type": type_, "label": type_, "params": params, "initiated_by": "someone-else"})


DEVICE_A = {"Authorization": "Bearer key-a"}


# --- the commands ------------------------------------------------------------


def test_the_five_are_wired_and_need_the_intended_capabilities():
    assert set(SITE_SETUP) <= WIRED_COMMAND_TYPES
    assert command_capabilities("START_SURVEY") == ("configure",)
    assert command_capabilities("STOP_SURVEY") == ("configure",)
    # The same as the map editor's PUT moved, which it ends in.
    assert command_capabilities("FIND_MOVING") == ROUTES[("PUT", "/maps/{map_id}/floors/{index}/moved")].capabilities
    assert command_capabilities("SET_LIDAR_MOUNT") == ("configure",)
    assert command_capabilities("SET_CALIBRATION_MODE") == ("configure",)
    # A stop is never stamped with a time limit; starting and moving the mount are.
    assert command_ttl_s("STOP_SURVEY") is None
    assert command_ttl_s("START_SURVEY") == 10.0 and command_ttl_s("SET_LIDAR_MOUNT") == 10.0


def test_sign_in_off_sends_all_five_attributed_as_the_body_says(world):
    client = TestClient(backend.app)
    for type_ in SITE_SETUP:
        response = _command(client, type_, _params(type_, world["here"]))
        assert response.status_code == 200, (type_, response.text)
    assert [c.type for c in world["sent"]] == list(SITE_SETUP)
    assert {c.initiated_by for c in world["sent"]} == {"someone-else"}
    assert world["sent"][1].ttl_s is None  # STOP_SURVEY


@pytest.mark.parametrize("user, allowed", [
    ("eve", set(SITE_SETUP)),               # engineer: configure
    ("sue", {"FIND_MOVING"}),               # supervisor: editZones only
    ("ann", set()), ("tom", set()),         # operator, technician
    ("vic", set()),                         # viewer: not even the command route
    ("far", set()),                         # an engineer, at another site
])
def test_who_may_send_each(auth_on, world, user, allowed):
    client = TestClient(backend.app)
    headers = _bearer(client, user)
    for type_ in SITE_SETUP:
        response = _command(client, type_, _params(type_, world["here"]), headers=headers)
        assert response.status_code == (200 if type_ in allowed else 403), (user, type_, response.text)


def test_a_refusal_names_the_type(auth_on, world):
    client = TestClient(backend.app)
    response = _command(client, "START_SURVEY", _params("START_SURVEY"), headers=_bearer(client, "sue"))
    assert response.json()["detail"] == "your role does not allow START_SURVEY"


def test_a_command_is_attributed_to_the_signed_in_engineer(auth_on, world):
    client = TestClient(backend.app)
    eve = auth_on.users.check_credentials("eve", PASSWORD)
    response = _command(client, "SET_LIDAR_MOUNT", MOUNT, headers=_bearer(client, "eve"))
    assert response.status_code == 200
    command = world["sent"][-1]
    assert command.initiated_by != "someone-else"
    assert command.initiated_by == response.json()["initiated_by"] == eve.user_id


def test_a_device_key_cannot_send_commands(auth_on, world):
    client = TestClient(backend.app)
    assert _command(client, "START_SURVEY", _params("START_SURVEY"), headers=DEVICE_A).status_code == 403


@pytest.mark.parametrize("type_, params, words", [
    ("START_SURVEY", {"name": "Office"}, "START_SURVEY params are not a survey to start (survey_id)"),
    ("START_SURVEY", {"survey_id": "s", "name": "x", "cameras": "all"},
     "START_SURVEY params are not a survey to start (cameras)"),
    ("STOP_SURVEY", {}, "STOP_SURVEY params are not a survey to stop (survey_id)"),
    ("FIND_MOVING", {"survey_id": "s", "map_id": "m", "sensitivity": 2},
     "FIND_MOVING params are not a moving-object search (sensitivity)"),
    ("SET_LIDAR_MOUNT", {"mount": {"x_m": 3, "y_m": 0, "z_m": 0}},
     "SET_LIDAR_MOUNT params are not a lidar mount (mount.x_m)"),
    ("SET_CALIBRATION_MODE", {"mode": "always"},
     "SET_CALIBRATION_MODE params are not a calibration mode (off, suggest or auto) (mode)"),
])
def test_unusable_params_are_a_422_in_plain_words(world, type_, params, words):
    response = _command(TestClient(backend.app), type_, params)
    assert response.status_code == 422
    assert response.json()["detail"] == words
    assert world["sent"] == []


@pytest.mark.parametrize("type_", ["START_SURVEY", "FIND_MOVING", "SET_LIDAR_MOUNT", "SET_CALIBRATION_MODE"])
def test_a_bridge_older_than_0_0_23_is_refused_with_the_reason(world, type_):
    response = _command(TestClient(backend.app), type_, _params(type_, world["here"]), robot="rover-old")
    assert response.status_code == 409
    assert response.json()["detail"] == ("update the edge device: it runs schema 0.0.22, surveys and lidar "
                                         "calibration need %s" % SURVEY_SCHEMA_VERSION)
    assert world["sent"] == []


def test_a_bridge_that_never_said_its_version_is_refused_too(world):
    backend.registry.upsert_from_registration("rover-mute", SITE, "quad")
    response = _command(TestClient(backend.app), "SET_CALIBRATION_MODE", {"mode": "off"}, robot="rover-mute")
    assert response.status_code == 409 and "runs schema unknown" in response.json()["detail"]


def test_stop_survey_is_never_refused_on_the_version(world):
    response = _command(TestClient(backend.app), "STOP_SURVEY", _params("STOP_SURVEY"), robot="rover-old")
    assert response.status_code == 200


def test_find_moving_needs_a_map_and_floor_at_the_robots_site(world):
    client = TestClient(backend.app)
    params = _params("FIND_MOVING", world["here"])
    assert _command(client, "FIND_MOVING", params).status_code == 200
    for map_id in ("map-nowhere", world["there"]):
        response = _command(client, "FIND_MOVING", {**params, "map_id": map_id})
        assert response.status_code == 404
        assert response.json()["detail"] == "map %s not found at this robot's site" % map_id
    response = _command(client, "FIND_MOVING", {**params, "floor_index": 3})
    assert response.status_code == 404
    assert response.json()["detail"] == "map %s has no floor 3" % world["here"]
    # The other site's robot may use the other site's map.
    assert _command(client, "FIND_MOVING", {**params, "map_id": world["there"]}, robot="rover-b").status_code == 200


def test_a_resurvey_names_a_map_at_the_robots_site(world):
    client = TestClient(backend.app)
    start = _params("START_SURVEY")
    assert _command(client, "START_SURVEY", start).status_code == 200  # a new map: no map_id
    assert _command(client, "START_SURVEY", {**start, "map_id": world["here"], "floor_index": 2}).status_code == 200
    assert _command(client, "START_SURVEY", {**start, "map_id": world["there"]}).status_code == 404
    assert _command(client, "START_SURVEY", {**start, "map_id": "map-nowhere"}).status_code == 404


# --- the broker --------------------------------------------------------------


def _acl(client, username, topic, acc):
    return client.post("/mqtt/acl", json={"username": username, "clientid": "c", "topic": topic,
                                          "acc": acc}).status_code


@pytest.mark.parametrize("suffix", ["survey/status", "survey/map", "sensors/lidar_mount", "nav/path"])
def test_a_device_publishes_its_own_survey_and_sensor_topics(auth_on, suffix):
    client = TestClient(backend.app)
    assert _acl(client, "rover-a", "roverhub/%s/rover-a/%s" % (SITE, suffix), ACC_WRITE) == 200
    # It may read its own retained status back (a bridge restarting mid-survey).
    assert _acl(client, "rover-a", "roverhub/%s/rover-a/%s" % (SITE, suffix), ACC_SUBSCRIBE) == 200
    assert _acl(client, "rover-a", "roverhub/%s/rover-b/%s" % (OTHER_SITE, suffix), ACC_WRITE) == 403
    # Not under another site's prefix once its own is known.
    assert _acl(client, "rover-a", "roverhub/%s/rover-a/%s" % (OTHER_SITE, suffix), ACC_WRITE) == 403


@pytest.mark.parametrize("suffix", ["survey/status", "survey/map", "sensors/lidar_mount", "nav/path"])
def test_people_read_them_at_their_sites_and_never_write(auth_on, suffix):
    client = TestClient(backend.app)
    uid = auth_on.users.check_credentials("vic", PASSWORD).user_id
    assert _acl(client, uid, "roverhub/+/+/%s" % suffix, ACC_SUBSCRIBE) == 200
    assert _acl(client, uid, "roverhub/%s/rover-a/%s" % (SITE, suffix), ACC_READ) == 200
    assert _acl(client, uid, "roverhub/%s/rover-b/%s" % (OTHER_SITE, suffix), ACC_READ) == 403
    assert _acl(client, uid, "roverhub/%s/rover-a/%s" % (SITE, suffix), ACC_WRITE) == 403


def test_neighbouring_topic_names_stay_closed(auth_on):
    client = TestClient(backend.app)
    for suffix in ("survey", "survey/other", "sensors", "sensors/other", "survey/#", "sensors/+", "nav", "nav/other", "nav/#"):
        assert _acl(client, "rover-a", "roverhub/%s/rover-a/%s" % (SITE, suffix), ACC_WRITE) == 403


# --- the robot uploads its own survey's draft (real app, sign-in on) ---------


def _post_survey(client, headers, site_id=SITE, map_id=None):
    form = {"site_id": site_id, "name": "Office"}
    if map_id is not None:
        form["map_id"] = map_id
    # A cloud that does not parse: the job fails at once in the background,
    # which is all an access test needs (the 202 and the job it returns).
    return client.post("/map-surveys", headers=headers, data=form,
                       files={"cloud": ("c.pcd", b"not a pcd", "application/octet-stream"),
                              "track": ("t.csv", b"t,x,y,z\n", "text/csv")})


def test_a_device_uploads_a_survey_for_its_own_site_and_polls_the_job(auth_on, world):
    client = TestClient(backend.app)
    map_id = "map-dev-%s" % uuid.uuid4().hex[:8]
    try:
        response = _post_survey(client, DEVICE_A, map_id=map_id)
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["site_id"] == SITE and job["requested_by"] == "rover-a"
        polled = client.get("/map-jobs/%s" % job["job_id"], headers=DEVICE_A)
        assert polled.status_code == 200 and polled.json()["job_id"] == job["job_id"]
        listed = client.get("/map-jobs", headers=DEVICE_A, params={"map_id": map_id}).json()["items"]
        assert [j["job_id"] for j in listed] == [job["job_id"]]
        # Another site's robot cannot see it; a person at the site can.
        assert client.get("/map-jobs/%s" % job["job_id"], headers={"Authorization": "Bearer key-b"}).status_code == 404
        assert client.get("/map-jobs", headers={"Authorization": "Bearer key-b"},
                          params={"map_id": map_id}).json()["items"] == []
        assert client.get("/map-jobs/%s" % job["job_id"], headers=_bearer(client, "vic")).status_code == 200
    finally:
        backend.maps_router.store.delete(map_id)


def test_a_device_may_not_upload_for_another_site_or_replace_its_map(auth_on, world):
    client = TestClient(backend.app)
    response = _post_survey(client, DEVICE_A, site_id=OTHER_SITE)
    assert response.status_code == 403
    assert response.json()["detail"] == "this device key belongs to rover-a at site %s, not site %s" % (
        SITE, OTHER_SITE)
    # Naming its own site but another site's map id: that map is not its to replace.
    assert _post_survey(client, DEVICE_A, map_id=world["there"]).status_code == 403


def test_a_device_whose_robot_has_not_registered_is_refused_with_the_reason(auth_on, world):
    client = TestClient(backend.app)
    headers = {"Authorization": "Bearer key-gone"}
    response = _post_survey(client, headers)
    assert response.status_code == 403
    assert response.json()["detail"].startswith("the cloud does not know which site rover-gone is at yet")
    assert client.get("/maps/%s" % world["here"], headers=headers).status_code == 403


def test_a_device_edits_and_reads_only_maps_at_its_site(auth_on, world):
    client = TestClient(backend.app)
    here, there = world["here"], world["there"]
    planning = pgm(planning_image())
    response = client.put("/maps/%s/floors/0/planning" % here, headers=DEVICE_A, content=planning)
    assert response.status_code == 200, response.text
    assert response.headers["X-Map-Version"] == "2"
    doc = moved_doc()
    doc["blobs"] = doc["blobs"][1:]
    assert client.put("/maps/%s/floors/0/moved" % here, headers=DEVICE_A,
                      content=json.dumps(doc)).status_code == 200
    assert client.get("/maps/%s" % here, headers=DEVICE_A).json()["map_id"] == here
    assert client.get("/maps/%s/floors/0/planning" % here, headers=DEVICE_A).content == planning
    assert client.get("/maps/%s/floors/0/moved" % here, headers=DEVICE_A).status_code == 200
    # Another site's map: refused, read or write.
    denied = "this device key belongs to rover-a at site %s, not site %s" % (SITE, OTHER_SITE)
    for response in (client.get("/maps/%s" % there, headers=DEVICE_A),
                     client.get("/maps/%s/floors/0/grid" % there, headers=DEVICE_A),
                     client.put("/maps/%s/floors/0/planning" % there, headers=DEVICE_A, content=b"P5"),
                     client.put("/maps/%s/floors/0/moved" % there, headers=DEVICE_A, content=b"{}")):
        assert response.status_code == 403 and response.json()["detail"] == denied


def test_a_device_still_may_not_do_what_only_people_do(auth_on, world):
    client = TestClient(backend.app)
    here = world["here"]
    for response in (client.get("/maps", headers=DEVICE_A),
                     client.delete("/maps/%s/floors/0/planning" % here, headers=DEVICE_A),
                     client.put("/maps/%s/status" % here, headers=DEVICE_A, json={"status": "active"}),
                     client.put("/maps/%s/zones" % here, headers=DEVICE_A, json={"zones": []}),
                     client.post("/maps/%s/floors/0/cloud-edits" % here, headers=DEVICE_A,
                                 json={"delete_boxes": [{"min": [0, 0, 0], "max": [1, 1, 1]}]}),
                     client.post("/maps/%s/versions/1/restore" % here, headers=DEVICE_A),
                     client.delete("/maps/%s" % here, headers=DEVICE_A),
                     client.post("/maps", headers=DEVICE_A)):
        assert response.status_code == 403
        assert response.json()["detail"] == "edge devices may not call this"


def test_unkeyed_bridges_never_reach_the_survey_routes(auth_on, world, monkeypatch):
    monkeypatch.setattr(auth_on, "allow_unkeyed_bridges", True)
    client = TestClient(backend.app)
    assert _post_survey(client, {}).status_code == 401
    assert client.put("/maps/%s/floors/0/moved" % world["here"], content=b"{}").status_code == 401
    assert client.get("/maps/%s" % world["here"]).status_code == 401
    assert client.get("/map-jobs/x", headers={"Authorization": "Bearer wrong-key"}).status_code == 401


def test_people_keep_their_rules_on_these_routes(auth_on, world):
    client = TestClient(backend.app)
    here = world["here"]
    assert _post_survey(client, _bearer(client, "sue")).status_code == 403     # no configure
    assert _post_survey(client, _bearer(client, "ann")).status_code == 403
    doc = json.dumps(moved_doc())
    assert client.put("/maps/%s/floors/0/moved" % here, headers=_bearer(client, "ann"), content=doc).status_code == 403
    assert client.put("/maps/%s/floors/0/moved" % here, headers=_bearer(client, "sue"), content=doc).status_code == 200
    assert client.get("/maps/%s" % here, headers=_bearer(client, "vic")).status_code == 200
    assert client.get("/maps/%s" % world["there"], headers=_bearer(client, "vic")).status_code == 403


def test_the_survey_routes_keep_their_rules():
    for key in (("POST", "/map-surveys"), ("PUT", "/maps/{map_id}/floors/{index}/planning"),
                ("PUT", "/maps/{map_id}/floors/{index}/moved"), ("GET", "/map-jobs/{job_id}"),
                ("GET", "/map-jobs"), ("GET", "/maps/{map_id}"), ("GET", "/maps/{map_id}/floors/{index}/{kind}"),
                ("GET", "/maps/{map_id}/floors/{index}/planning"), ("HEAD", "/maps/{map_id}")):
        rule = ROUTES[key]
        assert rule.device_own_site and not rule.device, key
    for key in (("GET", "/maps"), ("DELETE", "/maps/{map_id}/floors/{index}/planning"),
                ("PUT", "/maps/{map_id}/status"), ("POST", "/maps/{map_id}/floors/{index}/cloud-edits"),
                ("POST", "/maps"), ("DELETE", "/maps/{map_id}")):
        assert not ROUTES[key].device_own_site and not ROUTES[key].device, key


# --- the handlers with a device principal: audit rows, the survey's frame -----


class Audit:
    def __init__(self):
        self.rows = []

    def __call__(self, action, target, outcome, detail, **kw):
        self.rows.append({"action": action, "target": target, "detail": detail, **kw})

    def of(self, action):
        return [r for r in self.rows if r["action"] == action]


def _app_as(router, principal):
    app = FastAPI()

    @app.middleware("http")
    async def as_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


ROBOT = Principal(kind="device", id="rover-a", device_site=SITE)


@pytest.fixture(scope="module")
def survey_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("survey")
    return {"cloud": write_pcd(root / "room.pcd", room_cloud()), "track": track_bytes(room_track())}


@pytest.fixture
def surveyed(tmp_path, survey_files):
    """A pitched room surveyed and uploaded by rover-a's key: (router, client, audit)."""
    audit = Audit()
    router = build_maps_router(str(tmp_path / "maps"), audit=audit, jobs_dir=str(tmp_path / "jobs"),
                               start_jobs=False)
    client = _app_as(router, ROBOT)
    response = client.post("/map-surveys", data={"site_id": SITE, "name": "Room", "map_id": "map-room",
                                                 "survey_id": "s-1"},
                           files={"cloud": ("room.pcd", survey_files["cloud"].read_bytes(), "application/octet-stream"),
                                  "track": ("track.csv", survey_files["track"], "text/csv")})
    assert response.status_code == 202, response.text
    router.jobs.run_pending()
    assert router.jobs.get(response.json()["job_id"])["status"] == "succeeded"
    return router, client, audit


def test_the_audit_names_the_robot_that_uploaded(surveyed):
    router, client, audit = surveyed
    (row,) = audit.of("map.survey")
    assert row["detail"]["by_device"] == "rover-a" and row["robot_id"] == "rover-a" and row["site_id"] == SITE
    floor = router.store.get("map-room").floors[0]
    width, height = floor.size_cells
    planning = b"P5\n%d %d\n255\n" % (width, height) + bytes([254]) * (width * height)
    assert client.put("/maps/map-room/floors/0/planning", content=planning).status_code == 200
    (row,) = audit.of("map.planning.set")
    assert row["detail"]["by_device"] == "rover-a" and row["robot_id"] == "rover-a"


def test_a_person_s_audit_rows_name_no_robot(tmp_path):
    audit = Audit()
    router = build_maps_router(str(tmp_path / "maps"), audit=audit, jobs_dir=str(tmp_path / "jobs"),
                               start_jobs=False)
    router.store.save_bundle(make_room_bundle(tmp_path / "b"))
    client = _app_as(router, Principal(kind="dev", id="operator-dev"))
    assert client.put("/maps/%s/floors/0/moved" % MAP, content=json.dumps(moved_doc())).status_code == 200
    (row,) = audit.of("map.moved.set")
    assert "by_device" not in row["detail"] and "robot_id" not in row
    assert row["detail"]["frame"] == "map" and row["detail"]["moved_cloud_replaced"] is False


def _survey_rotation(router):
    survey = json.loads(router.store.artifact_path("map-room", 0, "survey").read_bytes())
    assert survey["levelling"]["rotated"] is True
    return np.asarray(survey["levelling"]["rotation"])


def test_a_rerun_in_the_surveys_frame_is_levelled_and_its_points_replace_the_old(surveyed):
    router, client, audit = surveyed
    rotation = _survey_rotation(router)
    doc = moved_doc()
    points = MOVED_POINTS + [3.0, 3.0, 0.0]
    response = client.put("/maps/map-room/floors/0/moved", params={"frame": "survey"},
                          files={"moved": ("moved.json", json.dumps(doc), "application/json"),
                                 "moved_cloud": ("moved.pcd", ce.pcd_bytes(points), "application/octet-stream")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["moved_cloud_replaced"] is True and body["blobs"] == 3 and body["map"]["status"] == "draft"
    stored = json.loads(router.store.artifact_path("map-room", 0, "moved").read_bytes())
    expected = ce.rotate_moved(ce.parse_moved(json.dumps(doc).encode()), rotation, 5.0)
    assert [b["bbox"] for b in stored["blobs"]] == [b["bbox"] for b in expected["blobs"]]
    stored_points = ce.read_pcd_bytes(router.store.artifact_path("map-room", 0, "moved_cloud").read_bytes(), 10**6)
    assert np.allclose(stored_points, points @ rotation.T, atol=1e-4)
    (row,) = audit.of("map.moved.set")
    assert row["detail"]["frame"] == "survey" and row["detail"]["by_device"] == "rover-a"


def test_a_text_moved_part_and_no_cloud_is_accepted(surveyed):
    router, client, _ = surveyed
    # A part with no filename is a text field (what curl -F moved=<moved.json sends).
    response = client.put("/maps/map-room/floors/0/moved", files={"moved": (None, json.dumps(moved_doc()))})
    assert response.status_code == 200, response.text
    assert response.json()["moved_cloud_replaced"] is False


@pytest.mark.parametrize("files, data, status, words", [
    ({"moved_cloud": ("m.pcd", b"x", "application/octet-stream")}, None, 400, "needs a 'moved' part"),
    ({"other": ("x", b"x", "text/plain")}, None, 400, "is not part of a moved layer upload"),
    ({"moved": (None, "{")}, None, 400, "not valid JSON"),
    ({"moved": ("m.json", json.dumps(moved_doc()), "application/json"),
      "moved_cloud": ("m.pcd", b"not a pcd", "application/octet-stream")}, None, 400, None),
])
def test_a_bad_multipart_moved_upload_is_refused(surveyed, files, data, status, words):
    _, client, _ = surveyed
    response = client.put("/maps/map-room/floors/0/moved", files=files, data=data)
    assert response.status_code == status, response.text
    if words:
        assert words in response.json()["detail"]


def test_the_surveys_frame_needs_a_floor_made_from_a_survey(tmp_path):
    router = build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"), start_jobs=False)
    router.store.save_bundle(make_room_bundle(tmp_path / "b"))
    client = _app_as(router, ROBOT)
    response = client.put("/maps/%s/floors/0/moved" % MAP, params={"frame": "survey"}, content=json.dumps(moved_doc()))
    assert response.status_code == 400
    assert "was not made from a survey upload" in response.json()["detail"]
    response = client.put("/maps/%s/floors/0/moved" % MAP, params={"frame": "lidar"}, content=json.dumps(moved_doc()))
    assert response.status_code == 400 and response.json()["detail"] == "frame must be one of: map, survey"


def test_a_device_principal_is_held_to_its_site_in_the_handler(tmp_path):
    router = build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"), start_jobs=False)
    client = _app_as(router, Principal(kind="device", id="rover-b", device_site=OTHER_SITE))
    response = client.post("/map-surveys", data={"site_id": SITE, "name": "Room"},
                           files={"cloud": ("c.pcd", b"x", "application/octet-stream"),
                                  "track": ("t.csv", b"x", "text/csv")})
    assert response.status_code == 403
    assert router.jobs.list(None) == []


# --- which survey and robot made a map (for "Find moving things again") -----


def _survey_block(client, map_id="map-room", version=None):
    path = ("/maps/%s/floors/0/survey" % map_id if version is None
            else "/maps/%s/versions/%d/floors/0/survey" % (map_id, version))
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()["survey"]


def test_a_devices_survey_keeps_its_survey_id_and_robot(surveyed):
    router, client, audit = surveyed
    assert _survey_block(client) == {"survey_id": "s-1", "robot_id": "rover-a"}
    # The newest succeeded survey job carries them too: what the console reads.
    jobs = client.get("/map-jobs", params={"map_id": "map-room"}).json()["items"]
    job = next(j for j in jobs if j["kind"] == "survey" and j["status"] == "succeeded")
    assert job["detail"]["survey_id"] == "s-1" and job["detail"]["robot_id"] == "rover-a"
    (row,) = audit.of("map.survey")
    assert row["detail"]["survey_id"] == "s-1" and row["detail"]["robot_id"] == "rover-a"


def test_the_survey_block_survives_edits_versions_and_restores(surveyed):
    router, client, _ = surveyed
    floor = router.store.get("map-room").floors[0]
    width, height = floor.size_cells
    planning = b"P5\n%d %d\n255\n" % (width, height) + bytes([254]) * (width * height)
    assert client.put("/maps/map-room/floors/0/planning", content=planning).status_code == 200
    assert client.put("/maps/map-room/floors/0/moved", content=json.dumps(moved_doc())).status_code == 200
    assert _survey_block(client) == {"survey_id": "s-1", "robot_id": "rover-a"}
    assert _survey_block(client, version=1) == {"survey_id": "s-1", "robot_id": "rover-a"}
    restored = _app_as(router, Principal(kind="dev", id="operator-dev")).post(
        "/maps/map-room/versions/1/restore")
    assert restored.status_code == 200, restored.text
    assert _survey_block(client) == {"survey_id": "s-1", "robot_id": "rover-a"}


def _person_upload(client, survey_files, **fields):
    form = {"site_id": SITE, "name": "Room", "map_id": "map-room", **fields}
    return client.post("/map-surveys", data=form,
                       files={"cloud": ("room.pcd", survey_files["cloud"].read_bytes(), "application/octet-stream"),
                              "track": ("track.csv", survey_files["track"], "text/csv")})


@pytest.fixture
def person(tmp_path):
    sites = {"rover-a": SITE, "rover-b": OTHER_SITE}
    router = build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"), start_jobs=False,
                               robot_site_of=sites.get)
    return router, _app_as(router, Principal(kind="dev", id="operator-dev"))


def test_a_person_may_name_a_robot_at_the_surveys_site(person, survey_files):
    router, client = person
    response = _person_upload(client, survey_files, survey_id="s-9", robot_id="rover-a")
    assert response.status_code == 202, response.text
    assert response.json()["detail"]["robot_id"] == "rover-a" and response.json()["detail"]["survey_id"] == "s-9"
    router.jobs.run_pending()
    assert _survey_block(client) == {"survey_id": "s-9", "robot_id": "rover-a"}


@pytest.mark.parametrize("robot_id", ["rover-b", "rover-nobody"])
def test_a_robot_elsewhere_or_unknown_is_refused(person, survey_files, robot_id):
    router, client = person
    response = _person_upload(client, survey_files, robot_id=robot_id)
    assert response.status_code == 400
    assert response.json()["detail"] == "robot_id %s is not a robot at site %s" % (robot_id, SITE)
    assert router.jobs.list(None) == []


def test_an_upload_by_hand_has_nulls(person, survey_files):
    router, client = person
    response = _person_upload(client, survey_files)
    assert response.status_code == 202
    assert response.json()["detail"]["survey_id"] is None and response.json()["detail"]["robot_id"] is None
    router.jobs.run_pending()
    assert _survey_block(client) == {"survey_id": None, "robot_id": None}


@pytest.mark.parametrize("survey_id", ["", "x" * 65])
def test_a_survey_id_out_of_bounds_is_refused(person, survey_files, survey_id):
    _, client = person
    response = _person_upload(client, survey_files, survey_id=survey_id)
    assert response.status_code == 400
    assert response.json()["detail"] == "survey_id must be 1 to 64 characters"


def test_a_device_cannot_claim_another_robot(tmp_path, survey_files):
    router = build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"), start_jobs=False)
    client = _app_as(router, ROBOT)
    response = _person_upload(client, survey_files, robot_id="rover-b")
    assert response.status_code == 400
    assert response.json()["detail"] == "robot_id must be this device's own robot (rover-a)"
    # Naming itself is fine.
    assert _person_upload(client, survey_files, robot_id="rover-a").status_code == 202


def test_the_app_checks_robot_id_against_the_registry(world):
    client = TestClient(backend.app)
    form = {"site_id": SITE, "name": "Office", "map_id": "map-x-%s" % uuid.uuid4().hex[:8]}
    files = {"cloud": ("c.pcd", b"not a pcd", "application/octet-stream"), "track": ("t.csv", b"t", "text/csv")}
    response = client.post("/map-surveys", data={**form, "robot_id": "rover-b"}, files=files)
    assert response.status_code == 400 and "not a robot at site" in response.json()["detail"]
    response = client.post("/map-surveys", data={**form, "robot_id": "rover-a", "survey_id": "s-2"}, files=files)
    assert response.status_code == 202, response.text
    assert response.json()["detail"]["robot_id"] == "rover-a"
    backend.maps_router.store.delete(form["map_id"])
