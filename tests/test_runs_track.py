"""GET /runs/{id}/track (runs/track.py): the run log's data for the run page."""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from maps.store import MapStore
from runs.router import build_runs_router
from shared.schemas.run_trace import TRACE_COLUMNS
from tests.run_trace_fixtures import MAP_ID, MISSION, RID, write_map, write_run, write_trace

TRACE_KEYS = {"source", "trace_version", "started_at", "duration_s", "columns", "plans", "legs", "log",
              "markers", "video", "map"}


def _epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _client(tmp_path, history=None, maps=True):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(exist_ok=True)
    store = None
    if maps:
        write_map(tmp_path / "maps")
        store = MapStore(tmp_path / "maps")
    calls = []

    def samples(robot_id, since, until):
        calls.append((robot_id, since, until))
        return history or []

    app = FastAPI()
    app.include_router(build_runs_router(str(runs_dir), map_store=store, history_samples=samples))
    return TestClient(app), runs_dir, calls


def test_track_from_a_trace_folder(tmp_path):
    client, runs_dir, calls = _client(tmp_path)
    write_trace(write_run(runs_dir))
    r = client.get(f"/runs/{RID}/track")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == TRACE_KEYS
    assert body["source"] == "trace" and body["trace_version"] == 1
    assert body["started_at"] == "2026-11-24T10:34:31Z"
    assert calls == []  # history is not asked when there is a trace
    columns = body["columns"]
    assert list(columns) == list(TRACE_COLUMNS)
    assert all(len(v) == 10 for v in columns.values())
    assert columns["t"][:3] == [0.0, 0.2, 0.4]
    assert columns["x"][2] is None and columns["y"][2] is None  # a blank cell is null, never 0
    assert columns["fwd_dropped"][1] == 2
    assert columns["controller"][0] == "FollowPath" and columns["controller"][8] is None
    assert columns["state"][6] == "E_STOP"
    assert columns["at"][0] == "2026-11-24T10:34:31Z" and columns["at"][1] is None
    assert body["duration_s"] == 1.8
    assert [p["goal_id"] for p in body["plans"]] == ["1", "2"]  # the bad line skipped
    assert body["plans"][0]["points"][1] == [2, 1.1]
    assert body["legs"][0]["walked_m"] == 3.1 and body["legs"][0]["waypoint_id"] == "wp-2"
    assert body["log"][0] == {"t": 0.05, "level": "INFO", "logger": "robot_bridge.nav_link",
                              "message": "goal 1 accepted"}
    assert body["log"][2] == {"t": None, "level": "", "logger": "", "message": "a line that is not tab separated"}
    assert body["video"] == {"front": {"path": "video/run_front.mp4", "offset_s": 1.5}}
    assert body["map"] == {"map_id": MAP_ID, "floor_index": 0}


def test_track_markers(tmp_path):
    client, runs_dir, _ = _client(tmp_path)
    write_trace(write_run(runs_dir))
    markers = client.get(f"/runs/{RID}/track").json()["markers"]
    got = [(m["t"], m["kind"], m["label"]) for m in markers]
    assert (0.0, "controller", "FollowPath") in got
    assert (0.6, "controller", "FollowPathTight") in got
    assert (1.0, "controller", "FollowPath") in got
    assert sum(1 for m in markers if m["kind"] == "controller") == 3  # blank is not a change
    assert (0.9, "stuck", "no progress for 10 s") in got
    assert (1.2, "estop", "E-Stop") in got
    assert (1.4, "estop_released", "E-Stop released") in got
    assert [m["kind"] for m in markers if m["kind"] in ("take_over", "control_back")] == ["take_over", "control_back"]
    take = next(m for m in markers if m["kind"] == "take_over")
    assert take["t"] == 0.8 and "user-1" in take["label"]
    assert [m["t"] for m in markers] == sorted(m["t"] for m in markers)


def test_video_without_a_first_byte_time_lines_up_at_zero(tmp_path):
    client, runs_dir, _ = _client(tmp_path)
    write_trace(write_run(runs_dir), meta=False)
    body = client.get(f"/runs/{RID}/track").json()
    assert body["source"] == "trace"
    assert body["video"] == {"front": {"path": "video/run_front.mp4", "offset_s": 0.0}}
    # No meta: t is counted from the run_started event, as the bridge counts it.
    assert body["started_at"] == "2026-11-24T10:34:31Z"


def test_no_video(tmp_path):
    client, runs_dir, _ = _client(tmp_path)
    write_trace(write_run(runs_dir), video=False)
    assert client.get(f"/runs/{RID}/track").json()["video"] == {}


def test_history_fallback(tmp_path):
    owner = f"mission:{RID}"
    rows = [
        {"at": "2026-11-24T10:34:31.5Z", "x_m": 1.0, "y_m": 1.0, "heading_deg": 90.0, "speed_mps": 0.0,
         "abstract_state": "AUTO_MISSION", "control_owner": owner},
        {"at": "2026-11-24T10:34:36.5Z", "x_m": 2.0, "y_m": 1.0, "heading_deg": 180.0, "speed_mps": 0.4,
         "abstract_state": "AUTO_MISSION", "control_owner": owner},
        {"at": "2026-11-24T10:34:41.5Z", "x_m": None, "y_m": None, "heading_deg": None, "speed_mps": None,
         "abstract_state": "E_STOP", "control_owner": owner},
        {"at": "2026-11-24T10:34:46.5Z", "x_m": 3.0, "y_m": 1.0, "heading_deg": 0.0, "speed_mps": 0.3,
         "abstract_state": "AUTO_MISSION", "control_owner": owner},
    ]
    client, runs_dir, calls = _client(tmp_path, history=rows)
    write_run(runs_dir)
    body = client.get(f"/runs/{RID}/track").json()
    assert set(body) == TRACE_KEYS
    assert body["source"] == "history" and body["trace_version"] is None
    assert body["started_at"] == "2026-11-24T10:34:31Z"  # the run_started event
    # Asked for this robot from the run_started event to the run's end.
    assert calls == [("rover-jetson", _epoch("2026-11-24T10:34:31Z"), _epoch("2026-11-24T10:35:31Z"))]
    columns = body["columns"]
    assert list(columns) == list(TRACE_COLUMNS)
    assert columns["t"] == [0.5, 5.5, 10.5, 15.5]
    assert columns["x"] == [1.0, 2.0, None, 3.0]
    assert abs(columns["yaw"][0] - 1.5707963) < 1e-6
    assert abs(columns["yaw"][1] - 3.1415926) < 1e-6
    assert columns["meas_vx"] == [0.0, 0.4, None, 0.3]
    assert columns["state"][2] == "E_STOP"
    assert columns["control_owner"][0] == owner
    assert columns["ask_vx"] == [None] * 4
    assert body["duration_s"] == 60.0
    assert body["plans"] == [] and body["legs"] == [] and body["log"] == [] and body["video"] == {}
    kinds = [m["kind"] for m in body["markers"]]
    assert kinds == ["estop", "estop_released"]
    assert body["map"] == {"map_id": MAP_ID, "floor_index": 0}


def test_history_fallback_for_a_run_still_going_asks_up_to_now(tmp_path):
    client, runs_dir, calls = _client(tmp_path)
    write_run(runs_dir, ended=False)
    body = client.get(f"/runs/{RID}/track").json()
    assert body["source"] == "none"
    assert calls and calls[0][2] > _epoch("2026-11-24T10:35:31Z")


def test_none_when_neither(tmp_path):
    client, runs_dir, _ = _client(tmp_path)
    write_run(runs_dir, events=False)
    body = client.get(f"/runs/{RID}/track").json()
    assert body["source"] == "none" and body["trace_version"] is None
    assert body["started_at"] == "2026-11-24T10:34:26Z"  # no run_started event: the run's own start
    assert all(v == [] for v in body["columns"].values()) and list(body["columns"]) == list(TRACE_COLUMNS)
    assert body["duration_s"] == 65.0
    assert body["markers"] == [] and body["video"] == {}


def test_history_failure_is_none_not_500(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    def broken(*_):
        raise RuntimeError("database gone")

    app = FastAPI()
    app.include_router(build_runs_router(str(runs_dir), history_samples=broken))
    write_run(runs_dir)
    body = TestClient(app).get(f"/runs/{RID}/track").json()
    assert body["source"] == "none"
    # No map store: the mission's map is named, floor 0.
    assert body["map"] == {"map_id": MAP_ID, "floor_index": 0}


def test_map_floor_is_the_one_at_the_waypoints_height(tmp_path):
    client, runs_dir, _ = _client(tmp_path, maps=False)
    write_map(tmp_path / "maps", floors=[{"index": 0, "floor_z_m": 0.0}, {"index": 1, "floor_z_m": 3.2}])
    app = FastAPI()
    app.include_router(build_runs_router(str(runs_dir), map_store=MapStore(tmp_path / "maps")))
    mission = {**MISSION, "waypoints": [{**w, "z_m": 3.1} for w in MISSION["waypoints"]]}
    write_run(runs_dir, mission=mission)
    assert TestClient(app).get(f"/runs/{RID}/track").json()["map"] == {"map_id": MAP_ID, "floor_index": 1}


def test_no_map(tmp_path):
    client, runs_dir, _ = _client(tmp_path)
    write_run(runs_dir, mission={**MISSION, "map_id": ""})
    assert client.get(f"/runs/{RID}/track").json()["map"] is None
    # A map the store does not have is no map either.
    write_run(runs_dir, run_id="task-abc-20261124T110000Z", mission={**MISSION, "map_id": "map-gone"})
    assert client.get("/runs/task-abc-20261124T110000Z/track").json()["map"] is None


def test_unknown_run_404(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get(f"/runs/{RID}/track").status_code == 404
