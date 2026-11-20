"""Map ready checklist (schema 0.0.29), the backend's part of spec §4.1:

- START_SURVEY is refused at once, in plain words, for a floor the map could
  never take (a new map's floor 2 used to be refused only after 20 minutes of
  recording and processing, by the SiteMap validator);
- a survey upload's `floor_label` is stored as the new floor's label.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

np = pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from maps.router import build_maps_router  # noqa: E402
from tests import test_site_setup as site_setup  # noqa: E402
from tests.test_maps_pipeline import room_cloud, room_track, track_bytes, write_pcd  # noqa: E402
from tests.test_site_setup import ROBOT, SITE, Audit, _app_as, _command, _params  # noqa: E402

# rover-a at the site on the current schema, a one-floor map there (["here"]),
# commands captured in ["sent"] rather than published.
world = site_setup.world


def test_new_map_survey_must_start_at_floor_0(world):
    client = TestClient(backend.app)
    start = _params("START_SURVEY")
    response = _command(client, "START_SURVEY", {**start, "floor_index": 2})
    assert response.status_code == 422, response.text
    assert "a new map starts at floor 0" in response.json()["detail"]
    assert world["sent"] == []
    assert _command(client, "START_SURVEY", {**start, "floor_index": 0}).status_code == 200


def test_existing_map_floor_may_be_next_but_not_beyond(world):
    client = TestClient(backend.app)
    start = {**_params("START_SURVEY"), "map_id": world["here"]}  # floors [0]
    assert _command(client, "START_SURVEY", {**start, "floor_index": 0}).status_code == 200
    assert _command(client, "START_SURVEY", {**start, "floor_index": 1}).status_code == 200
    response = _command(client, "START_SURVEY", {**start, "floor_index": 3})
    assert response.status_code == 422, response.text
    assert "floors 0–1" in response.json()["detail"]
    assert len(world["sent"]) == 2


@pytest.fixture(scope="module")
def survey_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("survey")
    return {"cloud": write_pcd(root / "room.pcd", room_cloud()), "track": track_bytes(room_track())}


def _upload(router, client, survey_files, **fields):
    response = client.post("/map-surveys", data={"site_id": SITE, "name": "Room", "map_id": "map-room", **fields},
                           files={"cloud": ("room.pcd", survey_files["cloud"].read_bytes(), "application/octet-stream"),
                                  "track": ("track.csv", survey_files["track"], "text/csv")})
    assert response.status_code == 202, response.text
    router.jobs.run_pending()
    job = router.jobs.get(response.json()["job_id"])
    assert job["status"] == "succeeded", job
    return job


def _router(tmp_path):
    router = build_maps_router(str(tmp_path / "maps"), audit=Audit(), jobs_dir=str(tmp_path / "jobs"),
                               start_jobs=False)
    return router, _app_as(router, ROBOT)


def test_floor_label_is_stored_on_the_new_floor(tmp_path, survey_files):
    router, client = _router(tmp_path)
    _upload(router, client, survey_files, floor_label="2F")
    floors = client.get("/maps/map-room").json()["floors"]
    assert floors[0]["label"] == "2F"


def test_a_resurvey_without_a_label_keeps_the_floors_label(tmp_path, survey_files):
    router, client = _router(tmp_path)
    _upload(router, client, survey_files, floor_label="Ground")
    _upload(router, client, survey_files)
    assert client.get("/maps/map-room").json()["floors"][0]["label"] == "Ground"
    _upload(router, client, survey_files, floor_label="Lobby")
    assert client.get("/maps/map-room").json()["floors"][0]["label"] == "Lobby"


def test_a_floor_label_over_40_characters_is_refused(tmp_path, survey_files):
    router, client = _router(tmp_path)
    response = client.post("/map-surveys", data={"site_id": SITE, "name": "Room", "floor_label": "x" * 41},
                           files={"cloud": ("room.pcd", b"x", "application/octet-stream"),
                                  "track": ("track.csv", b"t,x,y,z\n", "text/csv")})
    assert response.status_code == 400
    assert response.json()["detail"] == "floor_label must be at most 40 characters"
