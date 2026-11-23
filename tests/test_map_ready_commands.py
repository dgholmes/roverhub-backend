"""The map ready checklist (schema 0.0.29) through POST /robots/{id}/commands:
REPROCESS_SURVEY, SET_SURVEY_KEEP, DELETE_SURVEY_RECORDING, SKIP_MOVING_SEARCH,
USE_NAV_MAP and SET_SITE -- who may send each, their params, the bridge-version
gate, and what each names (a recording on the robot, a map and floor, a site).
Also the registration's new fields reaching GET /robots, and the survey list
judging the take the map was made from."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.policy import command_capabilities  # noqa: E402
from control.router import MAP_READY_SCHEMA_VERSION  # noqa: E402
from shared.schemas.commands import WIRED_COMMAND_TYPES  # noqa: E402
from surveys.store import SurveyStore  # noqa: E402
from tests import test_site_setup as site_setup  # noqa: E402
from tests.test_site_setup import OTHER_SITE, PASSWORD, SITE, _bearer, _command  # noqa: E402

world = site_setup.world
auth_on = site_setup.auth_on

MAP_READY = ("REPROCESS_SURVEY", "SET_SURVEY_KEEP", "DELETE_SURVEY_RECORDING", "SKIP_MOVING_SEARCH",
             "USE_NAV_MAP", "SET_SITE")
SURVEY = "survey-20261120-113333-2881"


def _recording(survey_id=SURVEY, has_bag=True, **over):
    item = {"survey_id": survey_id, "bytes": 4_000_000_000, "has_bag": has_bag, "has_cameras": False,
            "keep": False, "processing": False, "started_at": "2026-11-20T11:33:33Z"}
    item.update(over)
    return item


def _register(robot_id="rover-a", site=SITE, schema="0.0.29", **extra):
    """A registration as the bridge publishes it, through the MQTT client."""
    payload = {"bridge_id": "bridge-%s" % robot_id, "site_id": site, "robot_id": robot_id,
               "robot_type": "quad", "schema_version": schema, **extra}
    backend.mqtt_client.handle_message("roverhub/admin/bridge-%s/register" % robot_id, json.dumps(payload).encode())


def _floor_hash(map_id, index=0):
    floor = next(f for f in backend.maps_router.store.get(map_id).floors if f.index == index)
    return floor.content_hash


def _params(type_, world):
    return {
        "REPROCESS_SURVEY": {"survey_id": SURVEY},
        "SET_SURVEY_KEEP": {"survey_id": SURVEY, "keep": True},
        "DELETE_SURVEY_RECORDING": {"survey_id": SURVEY},
        "SKIP_MOVING_SEARCH": {"survey_id": SURVEY},
        "USE_NAV_MAP": {"map_id": world["here"], "floor_index": 0, "map_hash": _floor_hash(world["here"])},
        "SET_SITE": {"site_id": OTHER_SITE},
    }[type_]


@pytest.fixture
def listed(world):
    """rover-a lists SURVEY's recording, bag and all."""
    _register(recordings=[_recording()])
    return world


# --- wiring and permissions --------------------------------------------------


def test_all_six_are_wired_and_are_the_engineers():
    for type_ in MAP_READY:
        assert type_ in WIRED_COMMAND_TYPES, type_
        assert command_capabilities(type_) == ("configure",), type_
    assert MAP_READY_SCHEMA_VERSION == "0.0.29"


def test_sign_in_off_sends_all_six(listed):
    client = TestClient(backend.app)
    for type_ in MAP_READY:
        response = _command(client, type_, _params(type_, listed))
        assert response.status_code == 200, (type_, response.text)
    assert [c.type for c in listed["sent"]] == list(MAP_READY)
    assert {c.ttl_s for c in listed["sent"]} == {10.0}


def test_delete_and_keep_need_configure(auth_on, listed):
    client = TestClient(backend.app)
    for user in ("ann", "sue", "tom"):  # operator, supervisor, technician
        for type_ in ("DELETE_SURVEY_RECORDING", "SET_SURVEY_KEEP"):
            response = _command(client, type_, _params(type_, listed), headers=_bearer(client, user))
            assert response.status_code == 403, (user, type_, response.text)
            assert response.json()["detail"] == "your role does not allow %s" % type_
    for type_ in ("DELETE_SURVEY_RECORDING", "SET_SURVEY_KEEP"):
        assert _command(client, type_, _params(type_, listed), headers=_bearer(client, "eve")).status_code == 200
    assert listed["sent"] and all(c.type in ("DELETE_SURVEY_RECORDING", "SET_SURVEY_KEEP") for c in listed["sent"])


@pytest.mark.parametrize("type_, params, words", [
    ("REPROCESS_SURVEY", {}, "REPROCESS_SURVEY params are not a survey to process again (survey_id)"),
    ("REPROCESS_SURVEY", {"survey_id": SURVEY, "floor_label": "x" * 41},
     "REPROCESS_SURVEY params are not a survey to process again (floor_label)"),
    ("SET_SURVEY_KEEP", {"survey_id": SURVEY}, "SET_SURVEY_KEEP params are not a recording to keep or not (keep)"),
    ("DELETE_SURVEY_RECORDING", {}, "DELETE_SURVEY_RECORDING params are not a recording to delete (survey_id)"),
    ("SKIP_MOVING_SEARCH", {}, "SKIP_MOVING_SEARCH params are not a moving-object search to skip (survey_id)"),
    ("USE_NAV_MAP", {"map_id": "m"}, "USE_NAV_MAP params are not a map to use (map_hash)"),
    ("SET_SITE", {"site_id": "a/b"}, "SET_SITE params are not a site to move to (site_id)"),
])
def test_unusable_params_are_a_422_in_plain_words(listed, type_, params, words):
    response = _command(TestClient(backend.app), type_, params)
    assert response.status_code == 422
    assert response.json()["detail"] == words
    assert listed["sent"] == []


# --- the bridge's version ----------------------------------------------------


@pytest.mark.parametrize("type_", MAP_READY)
def test_new_commands_need_schema_0_0_29(listed, type_):
    _register("rover-028", schema="0.0.28", recordings=[_recording()])
    response = _command(TestClient(backend.app), type_, _params(type_, listed), robot="rover-028")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail.startswith("update the edge device: it runs schema 0.0.28, ")
    assert detail.endswith(" needs 0.0.29")
    assert listed["sent"] == []


def test_a_bridge_that_never_said_its_version_is_refused(listed):
    backend.registry.upsert_from_registration("rover-mute", SITE, "quad")
    response = _command(TestClient(backend.app), "SKIP_MOVING_SEARCH", {"survey_id": SURVEY}, robot="rover-mute")
    assert response.status_code == 409 and "runs schema unknown" in response.json()["detail"]


# --- REPROCESS_SURVEY --------------------------------------------------------


def test_reprocess_unknown_survey_is_refused_when_the_robot_does_not_list_it(listed):
    client = TestClient(backend.app)
    response = _command(client, "REPROCESS_SURVEY", {"survey_id": "survey-gone"})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("the recording is no longer on the robot")
    # Listed, but its bag was deleted: the results stay, nothing to process.
    _register(recordings=[_recording(has_bag=False)])
    response = _command(client, "REPROCESS_SURVEY", {"survey_id": SURVEY})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("the recording is no longer on the robot")
    assert listed["sent"] == []


def test_reprocess_a_new_map_is_floor_0(listed):
    client = TestClient(backend.app)
    response = _command(client, "REPROCESS_SURVEY", {"survey_id": SURVEY, "floor_index": 2})
    assert response.status_code == 422
    assert response.json()["detail"].startswith("a new map starts at floor 0, not floor 2")
    ok = _command(client, "REPROCESS_SURVEY", {"survey_id": SURVEY, "floor_label": "2F", "rebuild": True})
    assert ok.status_code == 200, ok.text
    assert listed["sent"][-1].params == {"survey_id": SURVEY, "floor_label": "2F", "rebuild": True}


def test_reprocess_into_a_map_may_redo_a_floor_or_add_the_next(listed):
    client = TestClient(backend.app)
    here = listed["here"]
    count = len(backend.maps_router.store.get(here).floors)
    for index in range(count + 1):
        response = _command(client, "REPROCESS_SURVEY", {"survey_id": SURVEY, "map_id": here, "floor_index": index})
        assert response.status_code == 200, (index, response.text)
    response = _command(client, "REPROCESS_SURVEY",
                        {"survey_id": SURVEY, "map_id": here, "floor_index": count + 1})
    assert response.status_code == 422
    assert "would leave a gap" in response.json()["detail"]


def test_reprocess_into_a_map_at_another_site_is_not_found(listed):
    client = TestClient(backend.app)
    for map_id in ("map-nowhere", listed["there"]):
        response = _command(client, "REPROCESS_SURVEY", {"survey_id": SURVEY, "map_id": map_id})
        assert response.status_code == 404
        assert response.json()["detail"] == "map %s not found at this robot's site" % map_id


# --- USE_NAV_MAP --------------------------------------------------------------


def test_use_nav_map_refuses_a_map_at_another_site(listed):
    there = listed["there"]
    response = _command(TestClient(backend.app), "USE_NAV_MAP",
                        {"map_id": there, "floor_index": 0, "map_hash": "ab" * 32})
    assert response.status_code == 409
    assert "move the robot to %s first" % OTHER_SITE in response.json()["detail"]
    assert listed["sent"] == []


def test_use_nav_map_at_a_site_the_caller_cannot_see_reads_as_not_found(auth_on, listed):
    client = TestClient(backend.app)
    response = _command(client, "USE_NAV_MAP", {"map_id": listed["there"], "floor_index": 0, "map_hash": "ab" * 32},
                        headers=_bearer(client, "eve"))
    assert response.status_code == 404
    assert OTHER_SITE not in response.json()["detail"]


def test_use_nav_map_checks_map_floor_and_hash(listed):
    client = TestClient(backend.app)
    here = listed["here"]
    good = _params("USE_NAV_MAP", listed)
    response = _command(client, "USE_NAV_MAP", {**good, "map_id": "map-nowhere"})
    assert response.status_code == 404
    assert response.json()["detail"] == "map map-nowhere not found"
    response = _command(client, "USE_NAV_MAP", {**good, "floor_index": 3})
    assert response.status_code == 404
    assert response.json()["detail"] == "map %s has no floor 3" % here
    response = _command(client, "USE_NAV_MAP", {**good, "map_hash": "ab" * 32})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("the map changed since you opened it")
    assert listed["sent"] == []
    assert _command(client, "USE_NAV_MAP", good).status_code == 200


def test_use_nav_map_is_allowed_for_a_robot_on_another_map(listed):
    client = TestClient(backend.app)
    _register(nav_map_id="map-elsewhere", nav_map_hash="cd" * 32)
    response = _command(client, "USE_NAV_MAP", _params("USE_NAV_MAP", listed))
    assert response.status_code == 200, response.text
    # and for one on no map at all
    _register()
    assert _command(client, "USE_NAV_MAP", _params("USE_NAV_MAP", listed)).status_code == 200


# --- SET_SITE -----------------------------------------------------------------


def test_set_site_unknown_site_404(listed):
    response = _command(TestClient(backend.app), "SET_SITE", {"site_id": "site-nowhere"})
    assert response.status_code == 404
    assert response.json()["detail"] == "site site-nowhere not found"
    assert listed["sent"] == []


def test_set_site_unknown_and_known_targets_look_the_same_without_rights(auth_on, listed):
    # eve configures the robot's site and holds nothing at either target.
    client = TestClient(backend.app)
    headers = _bearer(client, "eve")
    unknown = _command(client, "SET_SITE", {"site_id": "site-nowhere"}, headers=headers)
    known = _command(client, "SET_SITE", {"site_id": OTHER_SITE}, headers=headers)
    assert unknown.status_code == known.status_code == 403
    # The detail echoes only the id the caller sent.
    assert unknown.json()["detail"].replace("site-nowhere", "X") == known.json()["detail"].replace(OTHER_SITE, "X")
    assert listed["sent"] == []


@pytest.mark.parametrize("type_", MAP_READY)
def test_a_device_key_cannot_send_any_of_the_six(auth_on, listed, type_):
    response = _command(TestClient(backend.app), type_, _params(type_, listed), headers=site_setup.DEVICE_A)
    assert response.status_code == 403
    assert response.json()["detail"] == "edge devices may not call this"
    assert listed["sent"] == []


def test_a_site_with_only_a_map_is_known(listed):
    # OTHER_SITE keeps a map; with rover-b forgotten nothing else names it.
    backend.registry.forget("rover-b")
    response = _command(TestClient(backend.app), "SET_SITE", {"site_id": OTHER_SITE})
    assert response.status_code == 200, response.text


def test_set_site_to_its_own_site_is_refused(listed):
    response = _command(TestClient(backend.app), "SET_SITE", {"site_id": SITE})
    assert response.status_code == 409
    assert response.json()["detail"] == "rover-a is already at site %s" % SITE


def test_a_site_is_known_by_the_roles_people_hold_there(auth_on, listed):
    client = TestClient(backend.app)
    auth_on.users.create(username="new", display_name="new", password=PASSWORD, org_role="member",
                         site_roles={SITE: "engineer", "site-new": "engineer"})
    response = _command(client, "SET_SITE", {"site_id": "site-new"}, headers=_bearer(client, "new"))
    assert response.status_code == 200, response.text


def test_set_site_needs_configure_on_both_sites(auth_on, listed):
    client = TestClient(backend.app)
    for name, roles in (("both", {SITE: "engineer", OTHER_SITE: "engineer"}),
                        ("half", {SITE: "engineer", OTHER_SITE: "supervisor"})):
        auth_on.users.create(username=name, display_name=name, password=PASSWORD, org_role="member",
                             site_roles=roles)
    # eve: engineer here, nothing there; half: a supervisor there
    for user in ("eve", "half"):
        response = _command(client, "SET_SITE", {"site_id": OTHER_SITE}, headers=_bearer(client, user))
        assert response.status_code == 403, (user, response.text)
        assert OTHER_SITE in response.json()["detail"]
    # far: engineer there, nothing here -- refused at the robot's own site
    response = _command(client, "SET_SITE", {"site_id": OTHER_SITE}, headers=_bearer(client, "far"))
    assert response.status_code == 403
    assert listed["sent"] == []
    response = _command(client, "SET_SITE", {"site_id": OTHER_SITE}, headers=_bearer(client, "both"))
    assert response.status_code == 200, response.text
    assert listed["sent"][-1].params == {"site_id": OTHER_SITE}


# --- the registration's new fields --------------------------------------------


def test_registration_recordings_reach_get_robots(world):
    _register(recordings=[_recording(), _recording("survey-b", has_bag=False, keep=True)],
              disk_free_gb=41.5, nav_localised=True)
    robot, = [r for r in TestClient(backend.app).get("/robots").json() if r["robot_id"] == "rover-a"]
    assert [r["survey_id"] for r in robot["recordings"]] == [SURVEY, "survey-b"]
    assert robot["recordings"][1] == _recording("survey-b", has_bag=False, keep=True)
    assert robot["disk_free_gb"] == 41.5
    assert robot["nav_localised"] is True


def test_an_older_registration_reports_nothing_rather_than_empty_guesses(world):
    _register(schema="0.0.28")
    robot = next(r for r in TestClient(backend.app).get("/robots").json() if r["robot_id"] == "rover-a")
    assert robot["recordings"] == [] and robot["disk_free_gb"] is None and robot["nav_localised"] is None


def test_a_bad_recording_is_dropped_and_the_rest_kept(world):
    _register(recordings=[_recording(), {"survey_id": "survey-x", "bytes": -1}, "junk"],
              disk_free_gb="lots", nav_localised="yes")
    robot = next(r for r in TestClient(backend.app).get("/robots").json() if r["robot_id"] == "rover-a")
    assert [r["survey_id"] for r in robot["recordings"]] == [SURVEY]
    assert robot["disk_free_gb"] is None and robot["nav_localised"] is None


def test_an_over_long_recording_is_dropped_not_a_500(world):
    # KeptRecording bounds survey_id and started_at at 64 (0.0.29).
    _register(recordings=[_recording("s" * 65), _recording(started_at="t" * 65), _recording("survey-b")])
    response = TestClient(backend.app).get("/robots")
    assert response.status_code == 200
    robot = next(r for r in response.json() if r["robot_id"] == "rover-a")
    assert [r["survey_id"] for r in robot["recordings"]] == ["survey-b"]


def test_a_new_registration_replaces_the_list(world):
    _register(recordings=[_recording()], disk_free_gb=3.0, nav_localised=False)
    _register(recordings=[])
    robot = next(r for r in TestClient(backend.app).get("/robots").json() if r["robot_id"] == "rover-a")
    assert robot["recordings"] == [] and robot["disk_free_gb"] is None and robot["nav_localised"] is None


# --- the survey list judges the map's take ------------------------------------


def _record(survey_id, map_take_id=None):
    from shared.schemas.survey_replay import SurveyRecord
    return SurveyRecord(survey_id=survey_id, name=survey_id, site_id=SITE, robot_id="rover-a",
                        started_at="2026-11-20T11:33:33Z", duration_s=600.0, map_take_id=map_take_id)


def _take(survey_id, take_id, first_bad_s, has_legs=True, legs_m=100.0):
    from shared.schemas.survey_replay import ReplayTake
    return ReplayTake(take_id=take_id, survey_id=survey_id, source="live" if take_id == "original" else "replay",
                      label=take_id, poses=100, first_bad_s=first_bad_s, take_m=110.0, legs_m=legs_m,
                      has_legs=has_legs, created_at="2026-11-20T12:00:00Z")


def test_survey_summary_judges_the_map_take(tmp_path):
    store = SurveyStore(str(tmp_path))
    store.put(_record("s-map", map_take_id="rerun-20261120T120000Z"))
    store.put_take(_take("s-map", "original", first_bad_s=310.0, legs_m=90.0))
    store.put_take(_take("s-map", "rerun-20261120T120000Z", first_bad_s=None, legs_m=120.0))
    store.put(_record("s-orig"))
    store.put_take(_take("s-orig", "original", first_bad_s=310.0, legs_m=90.0))
    store.put_take(_take("s-orig", "rerun-20261120T120000Z", first_bad_s=None, legs_m=120.0))
    # named, but that take was never uploaded (or was deleted): no verdict at
    # all -- the original's would judge a map it did not make (row 2 grey)
    store.put(_record("s-missing", map_take_id="rerun-20261120T130000Z"))
    store.put_take(_take("s-missing", "original", first_bad_s=310.0, legs_m=90.0))
    by_id = {s.survey_id: s for s in store.list()}
    assert by_id["s-map"].first_bad_s is None and by_id["s-map"].legs_m == 120.0
    assert by_id["s-orig"].first_bad_s == 310.0 and by_id["s-orig"].legs_m == 90.0
    missing = by_id["s-missing"]
    assert missing.has_legs is False and missing.first_bad_s is None and missing.legs_m is None
    assert missing.take_count == 1
    assert by_id["s-map"].take_count == 2
