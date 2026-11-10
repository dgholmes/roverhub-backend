"""Settle what a previous backend process left in flight (audit 2026-11-10).

A backend that stops while a mission is in flight leaves its occurrence
"dispatched"/"running" and its run "running" on disk. Some of that nothing in
a new process would ever close, and an in-flight occurrence makes the scheduler
skip every later patrol of that robot as robot_busy:

- an occurrence whose run already ended (the wedge the old scheduler wrote:
  "running" written over a run that had been refused or had finished);
- an occurrence whose run was never created (the process stopped between the
  scheduler's "dispatched" write and the run record);
- a run on a robot that never reports again.

reconcile.py (a changed boot id) and reaper.py (a run the same bridge stopped
naming) already close many orphaned runs once their robot reports. This adds the
start-up pass and covers the rest, with the reasons the code already uses:
`bridgeRestarted`, `noAnswer`, and `timeLimit` for a run older than its task's
running-time cap on a robot that never answered.

How a live run is told from an orphan. The registry is in memory, so straight
after start-up nothing is known about any robot -- silence proves nothing. A run
is judged only by what its robot says in /state after this process started:
- it is online and names the run as its active_run: the run is live, and is
  left alone for as long as it keeps naming it;
- it has reported, is online, and has not named the run for STATE_GRACE_S
  (counted from its first state message, or from the last time it named the
  run): failed, `bridgeRestarted` if its boot id differs from the run's, else
  `noAnswer`;
- it has not reported (or is offline): the run is left alone, unless it is older
  than its task's cap -- wall time since it started -- and STATE_GRACE_S has
  passed since start-up and since the robot last named it. Then `timeLimit`.
  The edge's cap counts running time only, so a run paused long enough on a
  robot that cannot reach the cloud could in principle still be held there; it
  is ended here only when nothing has vouched for it, and the robot's own state
  (registry.active_run) still keeps the robot busy for new starts.
A run with no task cap on a robot that never reports stays running: there is
nothing to judge it by, and it blocks nothing but that (absent) robot.

Only runs that were in flight when this process started are considered: runs
this process started belong to the dispatcher, the reaper and run_ended. And,
like the reaper, a run with neither a source nor a boot id is a browser-driven
run no bridge ever knew; it is left alone.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional, Set

from robots.registry import RobotRegistry
from runs import occurrences
from runs.reaper import _epoch
from runs.store import RunStore
from shared.schemas.runs import MissionRun
from tasks.store import TaskStore

logger = logging.getLogger("roverhub.runs")

# After a robot's first /state since start-up (or since it last named a run),
# how long a run it does not name is given before it is failed. The bridge
# publishes /state retained, on change and with every heartbeat (5 s), and
# names its active run in each, so this is six chances to say so.
STATE_GRACE_S = 30.0

NO_RUN_MESSAGE = (
    "the backend stopped before this run was created, so it was never sent to the robot"
)


class OrphanSettler:
    def __init__(
        self, registry: RobotRegistry, run_store: RunStore, task_store: TaskStore,
        fail: Callable[[str, str], None], clock_s: Callable[[], float] = time.time,
        grace_s: float = STATE_GRACE_S,
    ):
        self._registry = registry
        self._runs = run_store
        self._tasks = task_store
        # (run_id, reason): RunDispatcher.start_failed, as the reaper uses --
        # it settles the run, its occurrence and the bucket copy, and is a
        # no-op on a run that already ended.
        self._fail = fail
        self._clock_s = clock_s
        self._grace_s = grace_s
        self._guard = threading.Lock()
        self._started_s: Optional[float] = None
        self._pending: Set[str] = set()
        # robot_id -> when its first /state since start-up was applied.
        self._first_state: Dict[str, float] = {}
        # run_id -> the last time its robot's state named it.
        self._last_named: Dict[str, float] = {}

    def note_state(self, robot_id: str) -> None:
        """The on_state hook: a state message from this robot has been applied
        to the registry. One that arrived before it registered is not recorded,
        because the registry dropped it."""
        if self._registry.get(robot_id) is None:
            return
        with self._guard:
            self._first_state.setdefault(robot_id, self._clock_s())

    def start(self) -> None:
        """Once, at start-up, before the scheduler's first tick: settle what
        needs no robot to judge, and remember the in-flight runs to judge later."""
        self._started_s = self._clock_s()
        for occ in self._tasks.list_occurrences():
            if occ.status not in occurrences.IN_FLIGHT:
                continue
            if occ.run_id is not None and occ.run_id != occ.occurrence_id:
                # Every run this backend makes for an occurrence shares its id;
                # one that does not was written by hand, and is not ours to judge.
                logger.warning("occurrence %s names run %s; leaving it alone", occ.occurrence_id, occ.run_id)
                continue
            try:
                self._settle_occurrence(occ.occurrence_id)
            except Exception:
                logger.exception("could not settle in-flight occurrence %s at start-up", occ.occurrence_id)
        for run in self._runs.list():
            if run.result == "running" and (run.source is not None or run.bridge_boot_id is not None):
                with self._guard:
                    self._pending.add(run.run_id)

    def _settle_occurrence(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            settled = occurrences.fail(self._runs, self._tasks, run_id, NO_RUN_MESSAGE)
            if settled is not None and settled.status == "failed":
                logger.warning("occurrence %s failed at start-up: it has no run record", run_id)
        elif run.result != "running":
            settled = occurrences.settle(self._runs, self._tasks, run)
            if settled is not None:
                logger.warning(
                    "occurrence %s was left in flight by a run that had ended (%s); now %s",
                    run_id, run.result, settled.status)
        # A run still "running" is judged by sweep(), from what its robot says.

    def sweep(self) -> None:
        """The watchdog's call, every few seconds."""
        if self._started_s is None:
            return
        with self._guard:
            pending = list(self._pending)
        now = self._clock_s()
        for run_id in pending:
            try:
                if self._consider(run_id, now):
                    with self._guard:
                        self._pending.discard(run_id)
                        self._last_named.pop(run_id, None)
            except Exception:
                logger.exception("could not check orphaned run %s", run_id)

    def _consider(self, run_id: str, now: float) -> bool:
        """True once the run is no longer this pass's to judge."""
        run = self._runs.get(run_id)
        if run is None:
            return True
        if run.result != "running":
            # Ended some other way (run_ended, reconcile, the reaper). Its
            # occurrence is settled by whoever ended it; this is belt and braces.
            occurrences.settle(self._runs, self._tasks, run)
            return True
        robot = self._registry.get(run.robot_id)
        with self._guard:
            first = self._first_state.get(run.robot_id)
        # Only an online robot vouches: /state is retained, so a dead bridge's
        # last word arrives at start-up too, and keeps naming the run.
        if robot is not None and robot.online and (robot.active_run or {}).get("run_id") == run_id:
            with self._guard:
                self._last_named[run_id] = now
            return False
        with self._guard:
            named = self._last_named.get(run_id)
        if robot is not None and robot.online and first is not None:
            quiet_since = max(first, named) if named is not None else first
            if now - quiet_since < self._grace_s:
                return False
            if (robot.bridge_boot_id is not None and run.bridge_boot_id is not None
                    and robot.bridge_boot_id != run.bridge_boot_id):
                return self._end(run, "bridgeRestarted")
            return self._end(run, "noAnswer")
        # Nothing has answered for it. Only its cap can end it.
        if now - self._started_s < self._grace_s:
            return False
        if named is not None and now - named < self._grace_s:
            return False
        cap = self._cap_s(run)
        started = _epoch(run.started_at)
        if cap is not None and started is not None and now - started > cap:
            return self._end(run, "timeLimit")
        return False

    def _cap_s(self, run: MissionRun) -> Optional[float]:
        occ = self._tasks.get_occurrence(run.run_id)
        task = self._tasks.get_task(occ.task_id) if occ is not None else None
        if task is None or task.max_duration_min is None:
            return None
        return task.max_duration_min * 60.0

    def _end(self, run: MissionRun, reason: str) -> bool:
        logger.warning(
            "run %s on %s, left running by a previous backend process, failed: %s",
            run.run_id, run.robot_id, reason)
        self._fail(run.run_id, reason)
        return True
