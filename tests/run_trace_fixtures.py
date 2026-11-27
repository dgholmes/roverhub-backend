"""Shared fixtures for the run log tests: a run with a mission on a small map,
a trace folder written the way the bridge writes it, and a map store."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from shared.schemas.run_trace import TRACE_COLUMNS

RID = "task-abc-20261124T103426Z"
MAP_ID = "map-fixture"
STARTED = "2026-11-24T10:34:31Z"

MISSION = {
    "mission_id": "m-1", "name": "Pictlab", "type": "patrol", "kind": "route",
    "creator": "x", "updated_at": "2026-11-23T00:00:00Z", "map_id": MAP_ID, "site_id": "site-a",
    "waypoints": [
        {"waypoint_id": "wp-1", "label": "Waypoint 1", "x_m": 1.0, "y_m": 1.0, "z_m": 0.0, "tolerance_m": 0.5},
        {"waypoint_id": "wp-2", "label": "Waypoint 2", "x_m": 4.0, "y_m": 1.0, "z_m": 0.0, "tolerance_m": 0.5},
        {"waypoint_id": "wp-3", "label": "Waypoint 3", "x_m": 4.0, "y_m": 3.0, "z_m": 0.0, "tolerance_m": 0.3},
    ],
}


def run_body(run_id=RID, ended=True, mission=None, **extra):
    body = {"run_id": run_id, "mission_id": "m-1", "mission": mission or MISSION, "robot_id": "rover-jetson",
            "site_id": "site-a", "started_at": "2026-11-24T10:34:26Z", "started_by": "operator-dev"}
    if ended:
        body.update(ended_at="2026-11-24T10:35:31Z", result="completed")
    body.update(extra)
    return body


def write_run(runs_dir: Path, run_id=RID, events=True, **extra) -> Path:
    from shared.schemas.runs import MissionRun
    directory = runs_dir / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(MissionRun.model_validate(run_body(run_id, **extra)).model_dump_json(),
                                        encoding="utf-8")
    if events:
        (directory / "events.jsonl").write_text(json.dumps(
            {"run_id": run_id, "seq": 0, "at": STARTED, "kind": "run_started"}) + "\n", encoding="utf-8")
    return directory


def _row(**values):
    return [values.get(name, "") for name in TRACE_COLUMNS]


def write_trace(run_dir: Path, run_id=RID, video=True, meta=True):
    """A 10-row trace: walks along y=1 then up x=4, with a gap in the pose,
    a controller switch, a take-over and back, and an E-Stop and release."""
    trace = run_dir / "trace"
    trace.mkdir(parents=True, exist_ok=True)
    owner = f"mission:{run_id}"
    rows = [
        _row(t="0.00", at="2026-11-24T10:34:31Z", x="1.0", y="1.0", yaw="0.0", ask_vx="0.3", sent_vx="0.25",
             meas_vx="0.2", controller="FollowPath", goal_id="1", goal_state="accepted", state="AUTO_MISSION",
             control_owner=owner, estop="0", fwd_dropped="0"),
        _row(t="0.20", x="1.5", y="1.0", yaw="0.0", controller="FollowPath", goal_id="1", state="AUTO_MISSION",
             control_owner=owner, estop="0", fwd_dropped="2"),
        _row(t="0.40", controller="FollowPath", goal_id="1", state="AUTO_MISSION", control_owner=owner,
             estop="0", fwd_dropped="0"),  # no pose: blank cells
        _row(t="0.60", x="2.5", y="1.0", yaw="0.0", controller="FollowPathTight", goal_id="1",
             state="AUTO_MISSION", control_owner=owner, estop="0"),
        _row(t="0.80", x="3.0", y="1.0", yaw="0.0", controller="FollowPathTight", goal_id="1",
             state="MANUAL", control_owner="user-1", estop="0"),
        _row(t="1.00", x="3.5", y="1.0", yaw="0.0", controller="FollowPath", goal_id="2",
             state="AUTO_MISSION", control_owner=owner, estop="0"),
        _row(t="1.20", x="4.0", y="1.0", yaw="1.57", controller="FollowPath", goal_id="2",
             state="E_STOP", control_owner=owner, estop="1"),
        _row(t="1.40", x="4.0", y="1.5", yaw="1.57", controller="FollowPath", goal_id="2",
             state="AUTO_MISSION", control_owner=owner, estop="0"),
        _row(t="1.60", x="4.0", y="2.5", yaw="1.57", controller="", goal_id="0", state="AUTO_MISSION",
             control_owner=owner, estop="0"),
        _row(t="1.80", x="4.0", y="3.0", yaw="1.57", controller="", goal_id="0", state="AUTO_MISSION",
             control_owner=owner, estop="0"),
    ]
    with open(trace / "samples.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(TRACE_COLUMNS)
        writer.writerows(rows)
    (trace / "plans.jsonl").write_text(
        json.dumps({"t": 0.0, "goal_id": "1", "points": [[1, 1], [2, 1.1], [4, 1]]}) + "\n"
        + "not json\n"
        + json.dumps({"t": 1.0, "goal_id": "2", "failed": False, "points": [[3.5, 1], [4, 2], [4, 3]]}) + "\n",
        encoding="utf-8")
    (trace / "legs.jsonl").write_text(json.dumps({
        "t_start": 0.0, "t_end": 1.2, "goal_id": "1", "waypoint_id": "wp-2", "outcome": "succeeded",
        "duration_s": 1.2, "walked_m": 3.1, "first_plan_m": 3.0, "straight_m": 3.0, "turn_in_place_s": 0.0,
        "plans": 2, "route_changes": 0, "line": "walk score ..."}) + "\n", encoding="utf-8")
    (trace / "log.txt").write_text(
        "0.05\tINFO\trobot_bridge.nav_link\tgoal 1 accepted\n"
        "0.90\tWARNING\trobot_bridge.nav_supervisor\tnav stopped: no progress for 10 s\n"
        "a line that is not tab separated\n", encoding="utf-8")
    if meta:
        doc = {"trace_version": 1, "run_id": run_id, "robot_id": "rover-jetson", "map_id": MAP_ID,
               "started_at": STARTED, "ended_at": "2026-11-24T10:35:31Z", "sample_hz": 5.0, "samples": 10,
               "video_started_at": {"front": "2026-11-24T10:34:32.5Z"} if video else {}}
        (trace / "meta.json").write_text(json.dumps(doc), encoding="utf-8")
    if video:
        (run_dir / "video").mkdir(exist_ok=True)
        (run_dir / "video" / "run_front.mp4").write_bytes(b"mp4")


def write_map(maps_dir: Path, map_id=MAP_ID, floors=None) -> Path:
    """A 60 x 40 cell map at 0.1 m: origin (0, 0), free inside a wall frame,
    unknown outside x > 5 m. Row 0 of the PGM is the top (largest y)."""
    width, height, res = 60, 40, 0.1
    pixels = bytearray([254] * (width * height))
    for row in range(height):
        for col in range(width):
            if row in (0, height - 1) or col in (0, width - 1):
                pixels[row * width + col] = 0
            elif col > 50:
                pixels[row * width + col] = 205
    directory = maps_dir / map_id
    floors = floors or [{"index": 0, "floor_z_m": 0.0}]
    doc = {"map_id": map_id, "site_id": "site-a", "name": "Fixture", "source": "slam",
           "extent_min": [0, 0], "extent_max": [6, 4], "created_at": "2026-11-20T00:00:00Z",
           "updated_at": "2026-11-20T00:00:00Z", "status": "active", "floors": []}
    for floor in floors:
        index = floor["index"]
        doc["floors"].append({"index": index, "floor_z_m": floor["floor_z_m"], "resolution_m": res,
                              "origin_xy_m": [0.0, 0.0], "size_cells": [width, height],
                              "free_cells": 0, "occupied_cells": 0, "unknown_cells": 0})
        (directory / f"floor{index}").mkdir(parents=True, exist_ok=True)
        (directory / f"floor{index}" / "grid.pgm").write_bytes(
            f"P5\n{width} {height}\n255\n".encode() + bytes(pixels))
    (directory / "map.json").write_text(json.dumps(doc), encoding="utf-8")
    return directory

