"""Fail a run that stopped being named by its own bridge (edge mission runs spec 6.7).

reconcile.py covers a bridge that restarted. This covers the other way a run is
lost: the bridge process is the same, up and reporting, but it no longer lists
the run as active (its run_ended was lost, or the executor died). Without this
such a run stays "running" and blocks the robot forever.

Only a robot whose state can vouch is judged: online, reporting a boot id (a
0.0.11+ bridge), and the SAME boot id the run recorded. A run with no boot id
is a browser-driven one the bridge never knew, so it is never touched."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Dict, Optional

from robots.registry import RobotRegistry
from runs.store import RunStore
from shared.schemas.runs import MissionRun

logger = logging.getLogger("roverhub.runs")

# How long a running run may go unnamed in its robot's state before it is failed.
NAMED_GRACE_S = 120.0
REAP_REASON = "noAnswer"


def _epoch(iso: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class RunReaper:
    def __init__(
        self, registry: RobotRegistry, run_store: RunStore,
        fail: Callable[[str, str], None], clock_s: Callable[[], float] = time.time,
    ):
        self._registry = registry
        self._runs = run_store
        # (run_id, reason): settles the run, its occurrence and the bucket copy
        # -- RunDispatcher.start_failed, which is a no-op on an ended run.
        self._fail = fail
        self._clock_s = clock_s
        # run_id -> last time the robot's state was seen naming it (or, for a
        # run first met here, when we first met it: a backend restart forgets
        # this, and the grace period starts again rather than reaping at once).
        self._last_named: Dict[str, float] = {}

    def sweep(self) -> None:
        now = self._clock_s()
        seen = set()
        for run in self._runs.list():
            if run.result != "running" or run.bridge_boot_id is None:
                continue
            seen.add(run.run_id)
            try:
                self._consider(run, now)
            except Exception:
                logger.exception("could not check run %s for a lost bridge", run.run_id)
        # Forget runs that ended, so the table cannot grow without bound.
        for run_id in set(self._last_named) - seen:
            del self._last_named[run_id]

    def _consider(self, run: MissionRun, now: float) -> None:
        last = self._last_named.setdefault(run.run_id, now)
        robot = self._registry.get(run.robot_id)
        if (robot is None or not robot.online or robot.bridge_boot_id is None
                or robot.bridge_boot_id != run.bridge_boot_id):
            # Nothing to judge it by right now; the grace period restarts when
            # the robot can vouch again, instead of reaping on its return.
            self._last_named[run.run_id] = now
            return
        if (robot.active_run or {}).get("run_id") == run.run_id:
            self._last_named[run.run_id] = now
            return
        started = _epoch(run.started_at)
        if started is not None and now - started < NAMED_GRACE_S:
            return
        if now - last < NAMED_GRACE_S:
            return
        logger.warning("run %s failed: %s no longer reports it (%s)", run.run_id, run.robot_id, REAP_REASON)
        self._fail(run.run_id, REAP_REASON)
