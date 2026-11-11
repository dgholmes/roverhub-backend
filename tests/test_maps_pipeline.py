"""The map pipeline: survey upload and levelling (maps/survey.py), background
jobs (maps/jobs.py), the quality report and the freeze gate (maps/quality.py,
maps/router.py), and bundle versions (maps/store.py).

Synthetic surveys stand in for real ones: a room with a floor, four walls and
a ceiling, pitched like FAST-LIO2's raw frame, and a walked loop. The real
2026-11-09 office survey (2.76 M points, 4.84 deg pitch) was run through the
same pipeline by hand on 2026-11-12: levelled to 0.033 deg in about 3 s, every
blocking check passing -- see the numbers in maps/quality.py's docstring.
"""
from __future__ import annotations

import json
import math
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

np = pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.policy import ROUTES, role_can  # noqa: E402
from maps import survey_math as sm  # noqa: E402
from maps.jobs import JobRunner  # noqa: E402
from maps.quality import THRESHOLDS, compute_report, is_current, run_quality  # noqa: E402
from maps.router import build_maps_router  # noqa: E402
from maps.store import MAX_VERSIONS, MapStore, VersionNotFound  # noqa: E402
from maps.survey import SurveyRequest, level_survey, process_survey  # noqa: E402
from shared.schemas.maps import MapZone  # noqa: E402
from tests.map_bundles import bundle_parts, make_bundle  # noqa: E402

PITCH_DEG = 5.0


# --- synthetic surveys -------------------------------------------------------


def _pitch(deg: float):
    t = math.radians(deg)
    return np.array([[math.cos(t), 0, math.sin(t)], [0, 1, 0], [-math.sin(t), 0, math.cos(t)]])


def room_cloud(width=10.0, depth=8.0, pitch_deg=PITCH_DEG, seed=1):
    """A room in FAST-LIO2's raw frame: the floor at z = -0.5, walls to the
    2.8 m ceiling, all pitched by `pitch_deg` about y. Two returns per 5 cm
    floor cell, so the converter sees the whole floor."""
    rng = np.random.default_rng(seed)
    xs, ys = np.meshgrid(np.arange(0, width, 0.05), np.arange(0, depth, 0.05))
    floor = np.column_stack([xs.ravel(), ys.ravel(), np.full(xs.size, -0.5)])
    floor = np.vstack([floor + [0.01, 0.01, 0], floor + [0.03, 0.03, 0]])
    walls = []
    for z in np.arange(-0.5, 2.3, 0.05):
        for x in np.arange(0, width, 0.05):
            walls += [(x, 0.0, z), (x, depth, z)]
        for y in np.arange(0, depth, 0.05):
            walls += [(0.0, y, z), (width, y, z)]
    walls = np.array(walls)
    walls = np.vstack([walls] * 7)  # dense enough for --min-hits 6
    ceiling = floor[: len(floor) // 2] + [0, 0, 2.8]
    xyz = np.vstack([floor, walls, ceiling]) + rng.normal(0, 0.005, (len(floor) + len(walls) + len(ceiling), 3))
    return xyz @ _pitch(pitch_deg).T


def room_track(pitch_deg=PITCH_DEG, samples=400):
    """A loop walked 1.5 m in from the walls at lidar height, same frame."""
    t = np.linspace(0, 2 * math.pi, samples)
    xyz = np.column_stack([5 + 3.5 * np.cos(t), 4 + 2.5 * np.sin(t), np.full(samples, -0.05)])
    xyz = xyz @ _pitch(pitch_deg).T
    return np.column_stack([np.arange(samples) * 0.1, xyz])


def write_pcd(path, xyz, ascii=False):
    header = ("# .PCD v0.7\nVERSION 0.7\nFIELDS x y z intensity\nSIZE 4 4 4 4\nTYPE F F F F\nCOUNT 1 1 1 1\n"
              "WIDTH %d\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS %d\nDATA %s\n"
              % (len(xyz), len(xyz), "ascii" if ascii else "binary"))
    data = np.column_stack([xyz, np.ones(len(xyz))]).astype("<f4")
    with open(path, "wb") as handle:
        handle.write(header.encode("ascii"))
        if ascii:
            handle.write("".join("%f %f %f %f\n" % tuple(r) for r in data).encode("ascii"))
        else:
            handle.write(data.tobytes())
    return path


def track_bytes(track):
    return sm.track_csv_bytes(track)


@pytest.fixture(scope="module")
def survey_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("survey")
    return {"cloud": write_pcd(root / "room.pcd", room_cloud()),
            "track": track_bytes(room_track()),
            "level_track": track_bytes(room_track(pitch_deg=0.0))}


def _process(store, files, tmp_path, track_key="track", **request):
    work = tmp_path / ("work-%d" % len(os.listdir(tmp_path)))
    work.mkdir()
    fields = {"map_id": "map-room", "site_id": "site-venue", "name": "Room", **request}
    return process_survey(store, SurveyRequest(**fields), files["cloud"], files[track_key], work)


# --- survey_math -------------------------------------------------------------


def test_read_pcd_reads_binary_and_ascii_alike(tmp_path):
    xyz = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.5]])
    binary = sm.read_pcd_xyz(write_pcd(tmp_path / "b.pcd", xyz))
    text = sm.read_pcd_xyz(write_pcd(tmp_path / "a.pcd", xyz, ascii=True))
    assert np.allclose(binary, xyz) and np.allclose(text, xyz)


@pytest.mark.parametrize("header, message", [
    ("FIELDS x y\nSIZE 4 4\nTYPE F F\nPOINTS 1\nDATA binary\n", "no x, y, z"),
    ("FIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nPOINTS 1\nDATA binary_compressed\n", "binary_compressed"),
    ("FIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nPOINTS 1000\nDATA binary\n", "shorter"),
    ("FIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nPOINTS 0\nDATA binary\n", "points"),
    ("hello\n", "DATA"),
])
def test_read_pcd_refuses_what_it_cannot_read(tmp_path, header, message):
    path = tmp_path / "bad.pcd"
    path.write_bytes(header.encode("ascii") + b"\x00" * 12)
    with pytest.raises(sm.SurveyError, match=message):
        sm.read_pcd_xyz(path)


@pytest.mark.parametrize("text, message", [
    (b"x,y,z\n1,2,3\n", "header"), (b"t,x,y,z\n", "no samples"), (b"t,x,y,z\n1,2,3\n", "four"),
    (b"t,x,y,z\n2,0,0,0\n1,0,0,0\n", "backwards"), (b"t,x,y,z\n1,nan,0,0\n", "finite"),
])
def test_read_track_refuses_a_bad_csv(text, message):
    with pytest.raises(sm.SurveyError, match=message):
        sm.read_track_csv(text)


def test_the_floor_fit_finds_the_pitch_and_the_rotation_removes_it():
    xyz = room_cloud()
    plane = sm.fit_floor_plane(xyz)
    assert plane["tilt_deg"] == pytest.approx(PITCH_DEG, abs=0.1)
    rotation, angle = sm.levelling_rotation(plane["a"], plane["b"])
    assert angle == pytest.approx(PITCH_DEG, abs=0.1)
    assert sm.fit_floor_plane(xyz @ rotation.T)["tilt_deg"] < 0.05


def test_the_track_plane_measures_tilt_and_drift():
    track = room_track()
    assert sm.track_plane(track[:, 1:4])["tilt_deg"] == pytest.approx(PITCH_DEG, abs=0.05)
    assert sm.track_plane(track[:, 1:4])["residual_p95_m"] < 0.001
    straight = np.column_stack([np.arange(10.0), np.zeros(10), np.zeros(10)])
    assert sm.track_plane(straight) is None


# --- levelling and the track's frame ---------------------------------------


def test_a_raw_track_is_rotated_with_the_cloud():
    out = level_survey(room_cloud(), room_track())
    assert out["info"]["rotated"] and out["info"]["track_frame"] == "raw"
    assert out["info"]["cloud_plane_after"]["tilt_deg"] < 0.05
    assert out["info"]["track_plane_after"]["tilt_deg"] < 0.15  # the cloud fit is good to ~0.1 deg


def test_a_levelled_track_is_left_alone():
    track = room_track(pitch_deg=0.0)
    out = level_survey(room_cloud(), track)
    assert out["info"]["track_frame"] == "level"
    assert np.allclose(out["track"], track)


def test_a_track_in_neither_frame_is_refused_rather_than_guessed():
    with pytest.raises(sm.SurveyError, match="neither"):
        level_survey(room_cloud(), room_track(pitch_deg=-4.0))


def test_a_level_cloud_is_not_rotated():
    out = level_survey(room_cloud(pitch_deg=0.0), room_track(pitch_deg=0.0))
    assert not out["info"]["rotated"]


def test_a_floor_steeper_than_a_mount_pitch_is_refused():
    with pytest.raises(sm.SurveyError, match="tilted"):
        level_survey(room_cloud(pitch_deg=20.0), room_track(pitch_deg=20.0))


# --- the survey pipeline -----------------------------------------------------


def test_a_survey_becomes_a_level_draft_map_with_a_passing_report(tmp_path, survey_files):
    store = MapStore(tmp_path / "maps")
    result = _process(store, survey_files, tmp_path)
    assert result["levelled"] and result["levelled_deg"] == pytest.approx(PITCH_DEG, abs=0.1)
    assert result["tilt_after_deg"] < 0.05
    site_map = store.get("map-room")
    assert site_map.status == "draft" and site_map.source == "slam"
    floor = site_map.floors[0]
    assert floor.free_cells > 10_000 and floor.occupied_cells > 600  # the four walls
    for kind in ("grid", "preview", "cloud", "track", "survey"):
        assert store.artifact_path("map-room", 0, kind) is not None, kind
    survey = json.loads(store.artifact_path("map-room", 0, "survey").read_text())
    assert survey["levelling"]["track_frame"] == "raw"
    report = store.read_quality("map-room")
    assert report["passed"] and is_current(store, "map-room", report)
    assert report["warnings"] == ["planning_grid"]
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses["floor_tilt_deg"] == statuses["track_tilt_deg"] == "pass"
    assert statuses["path_coverage"] == "skipped" and statuses["replay"] == "skipped"


def test_a_second_survey_keeps_the_zones_and_the_first_as_a_version(tmp_path, survey_files):
    store = MapStore(tmp_path / "maps")
    _process(store, survey_files, tmp_path)
    store.save_zones("map-room", [MapZone(zone_id="z1", map_id="map-room", kind="no_go", label="x",
                                          polygon=[(1, 1), (2, 1), (2, 2)])])
    store.set_status("map-room", "active")
    _process(store, survey_files, tmp_path, track_key="level_track", name="Room again")
    site_map = store.get("map-room")
    assert site_map.status == "draft" and [z.zone_id for z in site_map.zones] == ["z1"]
    assert [(v["version"], v["current"], v["source"]) for v in store.list_versions("map-room")] == [
        (2, True, "survey"), (1, False, "survey")]


def test_a_survey_of_one_floor_carries_the_others_over(tmp_path, survey_files):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b", map_id="map-room", floors=2, site_id="site-venue"))
    before = store.get("map-room").floors[1]
    _process(store, survey_files, tmp_path, floor_index=0)
    after = store.get("map-room")
    assert [f.index for f in after.floors] == [0, 1]
    assert after.floors[1].size_cells == before.size_cells
    assert store.artifact_path("map-room", 1, "grid").read_bytes() == \
        (tmp_path / "b" / "floor1" / "grid.pgm").read_bytes()


@pytest.mark.parametrize("change, message", [
    ({"map_id": ".."}, "map id"), ({"site_id": " "}, "site_id"), ({"resolution_m": 0.0}, "resolution"),
    ({"track_frame": "sideways"}, "track_frame"), ({"floor_index": -1}, "floor"),
])
def test_a_bad_survey_request_is_refused_before_anything_is_read(change, message):
    fields = {"map_id": "map-room", "site_id": "site-venue", "name": "Room", **change}
    with pytest.raises(sm.SurveyError, match=message):
        SurveyRequest(**fields).check()


# --- the quality report ------------------------------------------------------


def test_a_bundle_with_nothing_to_show_it_is_level_fails(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b"))
    report = compute_report(store, "map-venue")
    assert not report["passed"]
    assert "level_evidence" in report["failed"] and "free_area_m2" in report["failed"]
    assert {c["id"]: c["status"] for c in report["checks"]}["floor_tilt_deg"] == "skipped"


def test_a_track_outside_the_grid_fails(tmp_path, survey_files):
    store = MapStore(tmp_path / "maps")
    _process(store, survey_files, tmp_path)
    track = room_track(pitch_deg=0.0)
    track[:, 1] += 100.0
    (tmp_path / "maps" / "map-room" / "floor0" / "track.csv").write_bytes(track_bytes(track))
    report = compute_report(store, "map-room")
    assert "track_inside_grid" in report["failed"]


def test_path_coverage_comes_from_the_planning_grid(tmp_path, survey_files):
    store = MapStore(tmp_path / "maps")
    _process(store, survey_files, tmp_path)
    floor = store.get("map-room").floors[0]
    width, height = floor.size_cells
    free = b"P5\n%d %d\n255\n" % (width, height) + bytes([254]) * (width * height)
    store.set_planning("map-room", 0, free)
    assert not is_current(store, "map-room", store.read_quality("map-room"))  # a new input
    run_quality(store, "map-room")
    report = store.read_quality("map-room")
    assert report["floors"][0]["planning"]["path_coverage"] == 1.0 and report["passed"]

    # A wall across the room cuts the walked loop in two: coverage halves.
    image = bytearray(bytes([254]) * (width * height))
    for row in range(height):
        image[row * width + width // 2] = 0
    store.set_planning("map-room", 0, b"P5\n%d %d\n255\n" % (width, height) + bytes(image))
    run_quality(store, "map-room")
    report = store.read_quality("map-room")
    assert report["floors"][0]["planning"]["free_components"] == 2
    assert report["floors"][0]["planning"]["path_coverage"] < 0.6
    assert "path_coverage" in report["failed"]


def test_drawing_zones_does_not_make_the_report_stale(tmp_path, survey_files):
    store = MapStore(tmp_path / "maps")
    _process(store, survey_files, tmp_path)
    store.save_zones("map-room", [MapZone(zone_id="z1", map_id="map-room", kind="no_go", label="x",
                                          polygon=[(1, 1), (2, 1), (2, 2)])])
    assert is_current(store, "map-room", store.read_quality("map-room"))


def test_a_report_on_a_replaced_bundle_is_not_stored(tmp_path, survey_files):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b"))
    report = compute_report(store, "map-venue")
    store.save_bundle(make_bundle(tmp_path / "b2"))
    assert store.write_quality("map-venue", report, expect_version=report["version"]) is False
    assert store.read_quality("map-venue") is None


def test_the_thresholds_named_in_the_report_are_the_table():
    blocking = {k for k, v in THRESHOLDS.items() if v[2]}
    assert blocking == {"level_evidence", "floor_tilt_deg", "track_tilt_deg", "floor_below_fraction",
                        "free_area_m2", "track_inside_grid", "path_coverage"}


# --- versions ----------------------------------------------------------------


def test_a_reupload_keeps_the_old_bundle_and_restore_brings_it_back(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b1", name="First"))
    store.save_zones("map-venue", [MapZone(zone_id="z1", map_id="map-venue", kind="no_go", label="x",
                                           polygon=[(0, 0), (1, 0), (1, 1)])])
    store.save_bundle(make_bundle(tmp_path / "b2", name="Second"))
    versions = store.list_versions("map-venue")
    assert [(v["version"], v["current"], v["name"]) for v in versions] == [(2, True, "Second"), (1, False, "First")]
    assert store.version_artifact_path("map-venue", 1, 0, "grid") is not None

    store.set_status("map-venue", "active")
    restored = store.restore_version("map-venue", 1)
    assert restored.name == "First" and restored.status == "draft"
    assert [z.zone_id for z in restored.zones] == ["z1"]  # the zones carry over
    assert store.get("map-venue").name == "First"
    entries = store.list_versions("map-venue")
    assert [(v["version"], v["source"], v["restored_from"]) for v in entries] == [
        (3, "restore", 1), (2, "upload", None), (1, "upload", None)]
    assert store.get("map-venue").floors[0].content_hash is not None


def test_a_version_is_untouched_by_later_writes_to_the_live_map(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b1"))
    store.save_bundle(make_bundle(tmp_path / "b2"))
    archived = store.version_artifact_path("map-venue", 1, 0, "grid").read_bytes()
    store.set_planning("map-venue", 0, b"P5\n4 2\n255\n" + bytes([254] * 8))
    store.save_zones("map-venue", [])
    assert store.version_artifact_path("map-venue", 1, 0, "grid").read_bytes() == archived
    assert store.version_artifact_path("map-venue", 1, 0, "planning") is None


def test_only_the_newest_versions_are_kept(tmp_path):
    store = MapStore(tmp_path / "maps")
    for i in range(MAX_VERSIONS + 3):
        store.save_bundle(make_bundle(tmp_path / ("b%d" % i)))
    numbers = [v["version"] for v in store.list_versions("map-venue")]
    assert numbers[0] == MAX_VERSIONS + 3 and len(numbers) == MAX_VERSIONS + 1
    assert min(numbers) == 3


def test_restoring_a_version_that_is_not_kept_raises(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b1"))
    for bad in (0, 1, 2, -1, True):
        with pytest.raises(VersionNotFound):
            store.restore_version("map-venue", bad)
    assert store.version_artifact_path("map-venue", 1, 0, "../map.json") is None


# --- jobs --------------------------------------------------------------------


def test_a_job_reports_its_result_or_its_error(tmp_path):
    jobs = JobRunner(tmp_path / "jobs", start=False)
    ok = jobs.submit("quality", "map-a", "site-a", lambda progress: {"n": 1})
    bad = jobs.submit("quality", "map-b", "site-a", lambda progress: (_ for _ in ()).throw(ValueError("no floor")))
    crash = jobs.submit("quality", "map-c", "site-a", lambda progress: 1 / 0)
    assert jobs.get(ok["job_id"])["status"] == "queued"
    jobs.run_pending()
    assert jobs.get(ok["job_id"])["status"] == "succeeded" and jobs.get(ok["job_id"])["result"] == {"n": 1}
    assert jobs.get(bad["job_id"])["error"] == "no floor"
    assert jobs.get(crash["job_id"])["error"] == "internal error; see the backend log"


def test_a_job_cut_off_by_a_restart_is_marked_failed(tmp_path):
    jobs = JobRunner(tmp_path / "jobs", start=False)
    job = jobs.submit("survey", "map-a", "site-a", lambda progress: {})
    (jobs.work_dir(job["job_id"]) / "cloud.pcd").write_bytes(b"x")
    again = JobRunner(tmp_path / "jobs", start=False)
    settled = again.get(job["job_id"])
    assert settled["status"] == "failed" and "restarted" in settled["error"]
    assert not (tmp_path / "jobs" / (job["job_id"] + ".work")).exists()


def test_job_ids_are_never_paths(tmp_path):
    jobs = JobRunner(tmp_path / "jobs", start=False)
    for bad in ("..", "../x", "mjob-../../x", "", None, "mjob-0123"):
        assert jobs.get(bad) is None


# --- the routes --------------------------------------------------------------


@pytest.fixture
def router(tmp_path):
    return build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"), start_jobs=False)


@pytest.fixture
def client(router):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _survey(client, files, **fields):
    form = {"site_id": "site-venue", "name": "Room", "map_id": "map-room", **fields}
    return client.post("/map-surveys", data=form,
                       files={"cloud": ("room.pcd", files["cloud"].read_bytes(), "application/octet-stream"),
                              "track": ("track.csv", files["track"], "text/csv")})


def test_a_survey_upload_is_a_job_the_console_polls(client, router, survey_files):
    response = _survey(client, survey_files)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued" and job["kind"] == "survey" and job["map_id"] == "map-room"
    router.jobs.run_pending()
    done = client.get("/map-jobs/%s" % job["job_id"]).json()
    assert done["status"] == "succeeded", done
    assert done["result"]["quality"]["passed"] is True
    assert client.get("/map-jobs", params={"map_id": "map-room"}).json()["items"][0]["job_id"] == job["job_id"]
    assert client.get("/maps/map-room").json()["status"] == "draft"
    quality = client.get("/maps/map-room/quality").json()
    assert quality["current"] is True and quality["passed"] is True
    assert client.get("/maps/map-room/floors/0/track").headers["content-type"].startswith("text/csv")
    assert not any(n.endswith(".work") for n in os.listdir(router.jobs.root))


@pytest.mark.parametrize("fields, files, status", [
    ({"site_id": ""}, None, 400),
    ({"name": ""}, None, 400),
    ({"floor": "one"}, None, 400),
    ({"extra": "x"}, None, 400),
    ({}, {"cloud"}, 400),
])
def test_a_survey_upload_missing_something_is_a_400(client, survey_files, fields, files, status):
    form = {"site_id": "site-venue", "name": "Room", **fields}
    parts = {"cloud": ("room.pcd", b"x", "application/octet-stream"), "track": ("t.csv", b"x", "text/csv")}
    if files:
        parts = {k: v for k, v in parts.items() if k in files}
    assert client.post("/map-surveys", data=form, files=parts).status_code == status


def test_a_survey_upload_over_the_limit_is_a_413(tmp_path, survey_files):
    app = FastAPI()
    app.include_router(build_maps_router(str(tmp_path / "maps"), jobs_dir=str(tmp_path / "jobs"),
                                         start_jobs=False, max_survey_bytes=1000))
    assert _survey(TestClient(app), survey_files).status_code == 413


def test_a_survey_that_cannot_be_read_fails_its_job_with_the_reason(client, router):
    response = client.post("/map-surveys", data={"site_id": "site-venue", "name": "Room"},
                           files={"cloud": ("x.pcd", b"not a pcd", "application/octet-stream"),
                                  "track": ("t.csv", b"t,x,y,z\n0,0,0,0\n", "text/csv")})
    router.jobs.run_pending()
    job = router.jobs.get(response.json()["job_id"])
    assert job["status"] == "failed" and "DATA" in job["error"]
    assert client.get("/maps/map-room").status_code == 404


def _upload(client, tmp_path, **bundle):
    parts = bundle_parts(make_bundle(tmp_path / ("bundle-%d" % len(os.listdir(tmp_path))), **bundle))
    return client.post("/maps", files={n: (n.rsplit("/", 1)[-1], d, "application/octet-stream")
                                       for n, d in parts.items()})


def test_freezing_needs_a_current_passed_report_or_an_override(client, router, tmp_path):
    response = _upload(client, tmp_path)
    assert response.status_code == 201
    job_id = response.headers["X-Quality-Job"]
    freeze = {"status": "active"}

    # Queued, not yet run: no report.
    refused = client.put("/maps/map-venue/status", json=freeze)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "quality_missing"

    router.jobs.run_pending()
    assert router.jobs.get(job_id)["status"] == "succeeded"
    refused = client.put("/maps/map-venue/status", json=freeze)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "quality_failed"
    assert "level_evidence" in refused.json()["detail"]["failed"]

    # A planning grid changes an input: the report is stale until it runs again.
    assert client.put("/maps/map-venue/floors/0/planning", content=b"P5\n4 2\n255\n" + bytes([254] * 8)).status_code == 200
    refused = client.put("/maps/map-venue/status", json={**freeze, "override_quality": True})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "quality_stale"
    router.jobs.run_pending()

    frozen = client.put("/maps/map-venue/status", json={**freeze, "override_quality": True, "reason": "bench map"})
    assert frozen.status_code == 200 and frozen.json()["status"] == "active"
    # Other transitions are not gated.
    assert client.put("/maps/map-venue/status", json={"status": "draft"}).status_code == 200


def test_a_passing_report_freezes_without_an_override(client, router, survey_files):
    _survey(client, survey_files)
    router.jobs.run_pending()
    assert client.put("/maps/map-room/status", json={"status": "active"}).status_code == 200


def test_quality_jobs_for_one_map_are_not_queued_twice(client, router, tmp_path):
    _upload(client, tmp_path)
    first = client.post("/maps/map-venue/quality")
    second = client.post("/maps/map-venue/quality")
    assert first.status_code == 202 and first.json()["job_id"] == second.json()["job_id"]
    assert client.post("/maps/map-nope/quality").status_code == 404
    assert client.get("/maps/map-venue/quality").status_code == 404
    router.jobs.run_pending()
    assert client.get("/maps/map-venue/quality").status_code == 200


def test_versions_are_listed_served_and_restored_over_http(client, router, tmp_path):
    _upload(client, tmp_path, name="First")
    _upload(client, tmp_path, name="Second")
    items = client.get("/maps/map-venue/versions").json()["items"]
    assert [(i["version"], i["current"]) for i in items] == [(2, True), (1, False)]
    assert client.get("/maps/map-venue/versions/1/floors/0/grid").status_code == 200
    assert client.get("/maps/map-venue/versions/9/floors/0/grid").status_code == 404
    restored = client.post("/maps/map-venue/versions/1/restore")
    assert restored.status_code == 200 and restored.json()["name"] == "First"
    assert client.post("/maps/map-venue/versions/9/restore").status_code == 404
    assert client.get("/maps/map-nope/versions").status_code == 404


def test_every_new_route_has_a_rule_and_the_rules_are_the_intended_ones(router):
    from fastapi.routing import APIRoute

    for route in router.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                assert (method, route.path) in ROUTES, (method, route.path)
    for key in [("GET", "/map-jobs"), ("GET", "/map-jobs/{job_id}"), ("GET", "/maps/{map_id}/quality"),
                ("GET", "/maps/{map_id}/versions"),
                ("GET", "/maps/{map_id}/versions/{version}/floors/{index}/{kind}")]:
        assert any(role_can("viewer", c) for c in ROUTES[key].capabilities), key
    for key in [("POST", "/map-surveys"), ("POST", "/maps/{map_id}/versions/{version}/restore")]:
        assert ROUTES[key].capabilities == ("configure",), key
    quality = ROUTES[("POST", "/maps/{map_id}/quality")].capabilities
    assert any(role_can("supervisor", c) for c in quality) and any(role_can("engineer", c) for c in quality)
    assert not any(role_can("operator", c) for c in quality)


# --- with sign-in on: the override is a capability --------------------------


@pytest.fixture
def auth_on(tmp_path, monkeypatch):
    from auth.tokens import TokenIssuer
    from auth.users import UserStore

    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    for username, role in (("eve", "engineer"), ("sue", "supervisor"), ("sam", "supervisor")):
        auth.users.create(username=username, display_name=username, password="correct horse 1",
                          org_role="member", site_roles={"site-venue": role} if username != "sam"
                          else {"site-other": "supervisor"})
    return auth


def test_only_a_supervisor_at_the_maps_site_may_override(auth_on, tmp_path):
    map_id = "map-gate-%d" % os.getpid()
    store = backend.maps_router.store
    store.save_bundle(make_bundle(tmp_path / "b", map_id=map_id))
    try:
        run_quality(store, map_id)
        client = TestClient(backend.app)

        def bearer(username):
            token = client.post("/auth/login", json={"username": username,
                                                     "password": "correct horse 1"}).json()["access_token"]
            return {"Authorization": "Bearer " + token}

        body = {"status": "active", "override_quality": True}
        assert client.put("/maps/%s/status" % map_id, json=body, headers=bearer("eve")).status_code == 403
        assert client.put("/maps/%s/status" % map_id, json=body, headers=bearer("sam")).status_code == 403
        assert client.put("/maps/%s/status" % map_id, json=body, headers=bearer("sue")).status_code == 200
        # Engineers start a quality check; supervisors too.
        assert client.post("/maps/%s/quality" % map_id, headers=bearer("eve")).status_code == 202
    finally:
        store.delete(map_id)
