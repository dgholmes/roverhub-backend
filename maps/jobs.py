"""Background jobs for the map pipeline: processing a survey, checking quality.

Levelling a 3-million-point cloud and building its grids takes seconds to
minutes and hundreds of MB of memory, so it cannot run on a request. A job is
queued by the request, which answers 202 with the job at once; the console
polls GET /map-jobs/{job_id} until it is `succeeded` or `failed`.

In-process, one worker thread, no new infrastructure: the backend is a single
process (the map store already assumes it owns its directory), and one job at a
time bounds the memory a burst of uploads can take. A queue or worker service
would buy horizontal scale nobody needs for a handful of surveys a month.

Each job is a JSON file, `<root>/<job_id>.json`, rewritten atomically as it
moves (queued -> running -> succeeded | failed), so a job's status survives a
restart of the backend. Its working files (an uploaded survey) live in
`<root>/<job_id>.work/` and are removed when it ends. A job that was queued or
running when the process stopped cannot be resumed -- its inputs went with
its worker -- so the next start marks it failed, says why, and removes its
working files. The newest MAX_KEPT finished jobs are kept.

A job's `work(progress)` returns a JSON-able result dict, or raises: a
ValueError's message (BundleError, SurveyError) is shown to the uploader as
is; anything else is logged and reported as an internal error, so a stack
trace never reaches the console.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("roverhub.maps.jobs")

JOB_KINDS = ("survey", "quality")
JOB_STATES = ("queued", "running", "succeeded", "failed")
JOB_ID_RE = re.compile(r"mjob-[0-9a-f]{16}")
MAX_KEPT = 200
_WORK_SUFFIX = ".work"

Progress = Callable[[str], None]
Work = Callable[[Progress], dict]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRunner:
    def __init__(self, root, start: bool = True):
        """`root` is created on the first job, not here, so a backend that
        never runs one leaves no trace. `start=False` (tests) queues jobs
        without running them until `run_pending()` is called."""
        self.root = Path(root)
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._work: Dict[str, Work] = {}
        self._done = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._start = start
        self._settle_interrupted()

    # --- records -------------------------------------------------------------

    def _path(self, job_id: str) -> Path:
        return self.root / (job_id + ".json")

    def work_dir(self, job_id: str) -> Path:
        """Where a job keeps its inputs. Created on demand; removed when the job ends."""
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("not a job id: %r" % (job_id,))
        path = self.root / (job_id + _WORK_SUFFIX)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write(self, job: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(dir=self.root, prefix="." + job["job_id"] + "-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(job, handle, indent=2)
            os.replace(staged, self._path(job["job_id"]))
        except BaseException:
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise

    def get(self, job_id: str) -> Optional[dict]:
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            return None
        try:
            return json.loads(self._path(job_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self, map_id: Optional[str] = None) -> List[dict]:
        """Newest first."""
        jobs = []
        if not self.root.is_dir():
            return jobs
        for name in os.listdir(self.root):
            if name.endswith(".json") and JOB_ID_RE.fullmatch(name[:-5]):
                job = self.get(name[:-5])
                if job is not None and (map_id is None or job.get("map_id") == map_id):
                    jobs.append(job)
        jobs.sort(key=lambda j: (j.get("created_at", ""), j["job_id"]), reverse=True)
        return jobs

    def active(self, kind: str, map_id: str) -> Optional[dict]:
        """A queued job of this kind for this map, if one is waiting: a second
        request for the same check joins it instead of queueing a duplicate.
        A running one is not joined -- it may have read the map before the
        change that prompted the new request."""
        for job in self.list(map_id):
            if job.get("kind") == kind and job.get("status") == "queued":
                return job
        return None

    def _settle_interrupted(self) -> None:
        """At start: a job left queued or running was cut off by the process
        stopping. Mark it failed, remove leftover working files, and keep only
        the newest MAX_KEPT records."""
        if not self.root.is_dir():
            return
        try:
            names = os.listdir(self.root)
        except OSError:
            logger.exception("could not list the map job directory %s", self.root)
            return
        for name in names:
            path = self.root / name
            try:
                if name.endswith(_WORK_SUFFIX) or (name.startswith(".") and name.endswith(".tmp")):
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink()
            except OSError:
                logger.warning("could not remove %s", path)
        jobs = self.list()
        for job in jobs:
            if job.get("status") in ("queued", "running"):
                job.update(status="failed", finished_at=_now(),
                           error="the backend restarted before this job finished; start it again")
                try:
                    self._write(job)
                except OSError:
                    logger.exception("could not settle map job %s", job["job_id"])
        for job in jobs[MAX_KEPT:]:
            try:
                self._path(job["job_id"]).unlink()
            except OSError:
                pass

    # --- running -------------------------------------------------------------

    def new_id(self) -> str:
        return "mjob-" + uuid.uuid4().hex[:16]

    def submit(self, kind: str, map_id: str, site_id: Optional[str], work: Work,
               requested_by: Optional[str] = None, detail: Optional[dict] = None,
               job_id: Optional[str] = None) -> dict:
        """Queue `work` and return the job record. `job_id` when the caller
        already made one (to put the inputs in its work_dir first)."""
        if kind not in JOB_KINDS:
            raise ValueError("not a job kind: %r" % (kind,))
        job = {
            "job_id": job_id or self.new_id(), "kind": kind, "map_id": map_id, "site_id": site_id,
            "status": "queued", "stage": "queued", "created_at": _now(),
            "started_at": None, "finished_at": None, "error": None, "result": None,
            "requested_by": requested_by, "detail": detail or {},
        }
        with self._lock:
            self._write(job)
            self._work[job["job_id"]] = work
        self._queue.put(job["job_id"])
        if self._start:
            self._ensure_thread()
        return job

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                # A daemon: a backend stopping mid-job does not wait for it;
                # the next start marks the job failed (_settle_interrupted).
                self._thread = threading.Thread(target=self._loop, name="map-jobs", daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run(job_id)
            except Exception:  # never let the worker die
                logger.exception("map job %s crashed the worker", job_id)

    def run_pending(self) -> None:
        """Run every queued job on this thread (tests, start=False)."""
        while True:
            try:
                job_id = self._queue.get_nowait()
            except queue.Empty:
                return
            self._run(job_id)

    def _run(self, job_id: str) -> None:
        with self._lock:
            work = self._work.pop(job_id, None)
        job = self.get(job_id)
        if job is None or work is None:
            return
        job.update(status="running", stage="started", started_at=_now())
        self._write(job)

        def progress(stage: str) -> None:
            job["stage"] = stage
            try:
                self._write(job)
            except OSError:
                logger.warning("could not record progress of map job %s", job_id)

        try:
            result = work(progress)
            job.update(status="succeeded", stage="done", result=result)
        except ValueError as exc:  # BundleError, SurveyError: written for the uploader
            job.update(status="failed", error=str(exc))
        except Exception:
            logger.exception("map job %s (%s of %s) failed", job_id, job["kind"], job["map_id"])
            job.update(status="failed", error="internal error; see the backend log")
        finally:
            job["finished_at"] = _now()
            shutil.rmtree(self.root / (job_id + _WORK_SUFFIX), ignore_errors=True)
            with self._lock:
                self._write(job)
                self._done.notify_all()

    def wait(self, job_id: str, timeout: float = 60.0) -> Optional[dict]:
        """Block until the job has finished (tests and the CLI). The job, or
        None if it is unknown; the last record seen if the timeout passes."""
        deadline = threading.TIMEOUT_MAX if timeout is None else timeout
        with self._lock:
            self._done.wait_for(
                lambda: (self.get(job_id) or {}).get("status") in ("succeeded", "failed", None),
                timeout=deadline)
        return self.get(job_id)
