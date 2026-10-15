"""File-backed run records (run-records spec §6, §7.2).

One directory per run under RUNS_DIR, laid out exactly as the spec's §6 so the
backend copy, the zip and the bucket copy are the same tree. No database: the
directory is re-read on every request, like missions/router.py.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable, List, Optional

from fastapi import HTTPException

from shared.schemas.runs import RUN_ID_RE, MissionRun, RunEvent

logger = logging.getLogger("roverhub.runs")

DEFAULT_RUNS_DIR = str(Path(__file__).resolve().parents[1] / "data" / "runs")
RUN_FILE = "run.json"
EVENTS_FILE = "events.jsonl"
MEDIA_DIRS = ("photos", "video", "audio", "detections")


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class RunStore:
    def __init__(self, runs_dir: str):
        self.root = runs_dir
        os.makedirs(runs_dir, exist_ok=True)
        self._locks: dict = {}
        self._locks_guard = threading.Lock()

    def lock(self, run_id: str) -> threading.RLock:
        """Per-run lock. Endpoints are sync, so FastAPI runs them on a threadpool;
        read-modify-write of a run's files must hold this. Re-entrant so a caller
        holding it can still call put/add_events."""
        with self._locks_guard:
            return self._locks.setdefault(run_id, threading.RLock())

    def run_dir(self, run_id: str) -> str:
        if not RUN_ID_RE.match(run_id):
            raise HTTPException(status_code=400, detail="invalid run_id")
        root = os.path.realpath(self.root)
        resolved = os.path.realpath(os.path.join(self.root, run_id))
        if os.path.dirname(resolved) != root:
            raise HTTPException(status_code=400, detail="invalid run_id")
        return resolved

    def get(self, run_id: str) -> Optional[MissionRun]:
        path = os.path.join(self.run_dir(run_id), RUN_FILE)
        try:
            with open(path, encoding="utf-8") as handle:
                return MissionRun.model_validate_json(handle.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable run %s: %s", run_id, exc)
            return None

    def put(self, run: MissionRun) -> None:
        directory = self.run_dir(run.run_id)
        with self.lock(run.run_id):
            os.makedirs(directory, exist_ok=True)
            _atomic_write(os.path.join(directory, RUN_FILE), run.model_dump_json(indent=2))

    def list(self, site_id=None, robot_id=None, mission_id=None, since=None) -> List[MissionRun]:
        runs = []
        for name in os.listdir(self.root):
            if not RUN_ID_RE.match(name) or not os.path.isdir(os.path.join(self.root, name)):
                continue
            run = self.get(name)
            if run is None or run.run_id != name:
                continue
            if site_id and run.site_id != site_id:
                continue
            if robot_id and run.robot_id != robot_id:
                continue
            if mission_id and run.mission_id != mission_id:
                continue
            if since and run.started_at < since:
                continue
            runs.append(run)
        runs.sort(key=lambda r: (r.started_at, r.run_id), reverse=True)
        return runs

    def events(self, run_id: str) -> List[RunEvent]:
        path = os.path.join(self.run_dir(run_id), EVENTS_FILE)
        out = {}
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = RunEvent.model_validate_json(line)
                    except ValueError as exc:
                        logger.warning("skipping bad event line in %s: %s", run_id, exc)
                        continue
                    out[event.seq] = event
        except FileNotFoundError:
            return []
        return [out[k] for k in sorted(out)]

    def add_events(self, run_id: str, events: Iterable[RunEvent]) -> int:
        path = os.path.join(self.run_dir(run_id), EVENTS_FILE)
        with self.lock(run_id):
            existing = {e.seq: e for e in self.events(run_id)}
            added = 0
            for event in events:
                if event.seq not in existing:
                    existing[event.seq] = event
                    added += 1
            if added:
                text = "".join(existing[k].model_dump_json() + "\n" for k in sorted(existing))
                _atomic_write(path, text)
        return added

    def files(self, run_id: str) -> List[dict]:
        base = self.run_dir(run_id)
        out = []
        for media_dir in MEDIA_DIRS:
            folder = os.path.join(base, media_dir)
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                full = os.path.join(folder, name)
                if os.path.isfile(full) and not name.startswith("."):
                    out.append({"path": f"{media_dir}/{name}", "size_bytes": os.path.getsize(full)})
        return out
