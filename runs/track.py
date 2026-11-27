"""A run's log as the run page reads it: GET /runs/{id}/track.

Built from the run's trace/ folder (shared/schemas/run_trace.py), written by
the bridge during the run. A run with no trace -- every run before the run log
existed -- falls back to the history database's telemetry samples (one per
~5 s), which is enough for a coarse walked line and the state/controller
markers, and is labelled `source: "history"` so the page can say so.

The shape is a contract the frontend builds against
(docs/superpowers/plans/2026-11-27-mission-run-log.md, Task 2):

    source         "trace" | "history" | "none"
    trace_version  int | null
    started_at     the moment `t` counts from (UTC ISO)
    duration_s     float
    columns        every TRACE_COLUMNS name -> one value per sample (null = unknown)
    plans          TracePlan[]
    legs           TraceLeg[]
    log            {t, level, logger, message}[]
    markers        {t, kind, label}[], sorted by t
    video          {camera: {path, offset_s}}
    map            {map_id, floor_index} | null
"""
from __future__ import annotations

import csv
import logging
import math
import os
import re
import statistics
import time
from typing import Any, Callable, Dict, List, Optional

from history.db import epoch
from shared.schemas.run_trace import (
    LEGS_FILE, LOG_FILE, META_FILE, PLANS_FILE, SAMPLES_FILE, TRACE_COLUMNS, TRACE_DIR,
    TraceLeg, TraceMeta, TracePlan,
)
from shared.schemas.runs import MissionRun, RunEvent

logger = logging.getLogger("roverhub.runs")

# Columns that hold text; every other column is a number.
TEXT_COLUMNS = frozenset({"at", "controller", "goal_state", "state", "control_owner"})
VIDEO_RE = re.compile(r"^run_([A-Za-z0-9_-]+)\.mp4\Z")
STUCK_RE = re.compile(r"\bnav stopped: (.*)$")
E_STOP = "E_STOP"
# Two floors closer than this in height are told apart by the nearer one; a
# route further than this from every floor is drawn on the map's first floor.
FLOOR_MATCH_M = 1.0

# (robot_id, since epoch, until epoch) -> history rows, oldest first.
HistorySamples = Callable[[str, float, float], List[Dict[str, Any]]]


def _number(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return int(value) if value.is_integer() and "." not in text and "e" not in text.lower() else value


def _empty_columns() -> Dict[str, list]:
    return {name: [] for name in TRACE_COLUMNS}


def _read_jsonl(path: str, model) -> List[dict]:
    out: List[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(model.model_validate_json(line).model_dump(mode="json"))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return out


def _read_samples(path: str) -> Dict[str, list]:
    columns = _empty_columns()
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if not header:
                return columns
            index = {name.strip(): i for i, name in enumerate(header)}
            for row in reader:
                if not row:
                    continue
                for name in TRACE_COLUMNS:
                    i = index.get(name)
                    cell = row[i] if i is not None and i < len(row) else None
                    if name in TEXT_COLUMNS:
                        columns[name].append(cell.strip() or None if cell is not None else None)
                    else:
                        columns[name].append(_number(cell))
    except FileNotFoundError:
        pass
    return columns


def _read_log(path: str) -> List[dict]:
    """log.txt: `<t>\\t<LEVEL>\\t<logger>\\t<message>`. A line that is not in
    that form is kept whole as the message, with no time."""
    out: List[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                parts = line.split("\t", 3)
                t = _number(parts[0]) if len(parts) == 4 else None
                if t is None:
                    out.append({"t": None, "level": "", "logger": "", "message": line})
                else:
                    out.append({"t": float(t), "level": parts[1], "logger": parts[2], "message": parts[3]})
    except FileNotFoundError:
        pass
    return out


def _read_meta(path: str) -> Optional[TraceMeta]:
    try:
        with open(path, encoding="utf-8") as handle:
            return TraceMeta.model_validate_json(handle.read())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("unreadable trace meta %s: %s", path, exc)
        return None


def run_started_at(run: MissionRun, events: List[RunEvent]) -> str:
    """When the bridge's run began: its run_started event, else the run's own
    start (the moment it was dispatched, a few seconds earlier)."""
    started = next((e.at for e in events if e.kind == "run_started"), None)
    return started or run.started_at


def mission_map(run: MissionRun, map_store=None) -> Optional[dict]:
    """{map_id, floor_index} of the run's mission map, or None. The floor is
    the one whose height is nearest the waypoints' (within FLOOR_MATCH_M),
    else the map's first floor. With no map store the floor is 0."""
    mission = run.mission
    map_id = getattr(mission, "map_id", None)
    if not map_id:
        return None
    if map_store is None:
        return {"map_id": map_id, "floor_index": 0}
    try:
        site_map = map_store.get(map_id)
    except Exception:
        logger.exception("could not read map %s for run %s", map_id, run.run_id)
        return None
    if site_map is None or not site_map.floors:
        return None
    floors = sorted(site_map.floors, key=lambda f: f.index)
    chosen = floors[0]
    heights = [w.z_m for w in (getattr(mission, "waypoints", None) or []) if w.z_m is not None]
    if heights:
        z = statistics.median(heights)
        nearest = min(floors, key=lambda f: abs(f.floor_z_m - z))
        if abs(nearest.floor_z_m - z) <= FLOOR_MATCH_M:
            chosen = nearest
    return {"map_id": map_id, "floor_index": chosen.index}


def _markers(columns: Dict[str, list], log: List[dict], run_id: str) -> List[dict]:
    out: List[dict] = []
    for line in log:
        if line["t"] is None:
            continue
        match = STUCK_RE.search(line["message"])
        if match:
            out.append({"t": line["t"], "kind": "stuck", "label": match.group(1).strip() or "stopped"})
    owner_id = f"mission:{run_id}"
    times = columns["t"]
    prev_state = prev_owner = prev_controller = None
    mission_seen = False
    for i, t in enumerate(times):
        if t is None:
            continue
        state = columns["state"][i]
        if state is not None:
            if state == E_STOP and prev_state != E_STOP:
                out.append({"t": t, "kind": "estop", "label": "E-Stop"})
            elif state != E_STOP and prev_state == E_STOP:
                out.append({"t": t, "kind": "estop_released", "label": "E-Stop released"})
            prev_state = state
        owner = columns["control_owner"][i]
        if i == 0 or owner != prev_owner:
            if owner == owner_id:
                if mission_seen and prev_owner != owner_id:
                    out.append({"t": t, "kind": "control_back", "label": "control back to the mission"})
                mission_seen = True
            elif prev_owner == owner_id:
                label = f"taken over by {owner}" if owner else "control released"
                out.append({"t": t, "kind": "take_over", "label": label})
            prev_owner = owner
        controller = columns["controller"][i]
        if controller is not None and controller != prev_controller:
            out.append({"t": t, "kind": "controller", "label": controller})
            prev_controller = controller
    out.sort(key=lambda m: m["t"])
    return out


def _span(run: MissionRun, started_at: str) -> float:
    start = epoch(started_at)
    end = epoch(run.ended_at) if run.ended_at else None
    if start is None or end is None:
        return 0.0
    return round(max(0.0, end - start), 3)


def _from_trace(run: MissionRun, events: List[RunEvent], run_dir: str) -> dict:
    trace = os.path.join(run_dir, TRACE_DIR)
    meta = _read_meta(os.path.join(trace, META_FILE))
    started_at = meta.started_at if meta is not None else run_started_at(run, events)
    columns = _read_samples(os.path.join(trace, SAMPLES_FILE))
    log = _read_log(os.path.join(trace, LOG_FILE))
    times = [t for t in columns["t"] if t is not None]
    duration = float(max(times)) if times else _span(run, started_at)
    video = {}
    folder = os.path.join(run_dir, "video")
    if os.path.isdir(folder):
        start = epoch(started_at)
        for name in sorted(os.listdir(folder)):
            match = VIDEO_RE.match(name)
            if not match or not os.path.isfile(os.path.join(folder, name)):
                continue
            camera = match.group(1)
            first = epoch((meta.video_started_at or {}).get(camera)) if meta is not None else None
            offset = round(first - start, 3) if first is not None and start is not None else 0.0
            video[camera] = {"path": f"video/{name}", "offset_s": offset}
    return {
        "source": "trace",
        "trace_version": meta.trace_version if meta is not None else None,
        "started_at": started_at,
        "duration_s": duration,
        "columns": columns,
        "plans": _read_jsonl(os.path.join(trace, PLANS_FILE), TracePlan),
        "legs": _read_jsonl(os.path.join(trace, LEGS_FILE), TraceLeg),
        "log": log,
        "markers": _markers(columns, log, run.run_id),
        "video": video,
    }


def _yaw(heading_deg) -> Optional[float]:
    if not isinstance(heading_deg, (int, float)) or not math.isfinite(heading_deg):
        return None
    return math.atan2(math.sin(math.radians(heading_deg)), math.cos(math.radians(heading_deg)))


def _num(value) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _from_history(run: MissionRun, events: List[RunEvent],
                  history_samples: Optional[HistorySamples], clock: Callable[[], float]) -> dict:
    started_at = run_started_at(run, events)
    columns = _empty_columns()
    rows: List[Dict[str, Any]] = []
    start = epoch(started_at)
    if history_samples is not None and start is not None:
        end = epoch(run.ended_at) if run.ended_at else clock()
        try:
            rows = list(history_samples(run.robot_id, start, end)) if end is not None and end > start else []
        except Exception:
            logger.exception("could not read history samples for run %s", run.run_id)
            rows = []
    for row in rows:
        at = epoch(row.get("at"))
        if at is None:
            continue
        for name in TRACE_COLUMNS:
            columns[name].append(None)
        columns["t"][-1] = round(at - start, 3) + 0.0  # never -0.0
        columns["at"][-1] = row.get("at")
        columns["x"][-1] = _num(row.get("x_m"))
        columns["y"][-1] = _num(row.get("y_m"))
        columns["yaw"][-1] = _yaw(row.get("heading_deg"))
        columns["meas_vx"][-1] = _num(row.get("speed_mps"))
        columns["state"][-1] = row.get("abstract_state") or None
        columns["control_owner"][-1] = row.get("control_owner") or None
    source = "history" if columns["t"] else "none"
    duration = _span(run, started_at)
    if not duration and columns["t"]:
        duration = float(columns["t"][-1])
    return {
        "source": source, "trace_version": None, "started_at": started_at, "duration_s": duration,
        "columns": columns, "plans": [], "legs": [], "log": [],
        "markers": _markers(columns, [], run.run_id), "video": {},
    }


def has_trace(run_dir: str) -> bool:
    trace = os.path.join(run_dir, TRACE_DIR)
    return any(os.path.isfile(os.path.join(trace, name)) for name in (SAMPLES_FILE, META_FILE))


def build_track(run: MissionRun, events: List[RunEvent], run_dir: str,
                history_samples: Optional[HistorySamples] = None, map_store=None,
                clock: Callable[[], float] = time.time) -> dict:
    if has_trace(run_dir):
        track = _from_trace(run, events, run_dir)
    else:
        track = _from_history(run, events, history_samples, clock)
    track["map"] = mission_map(run, map_store)
    return track

