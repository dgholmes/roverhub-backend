"""Moving a task occurrence along with its run (audit 2026-11-10).

An occurrence shares its run's id, and several writers move it: the scheduler
after it publishes, run-now, a bridge's refusal (start_failed), a run_ended, a
bridge restart (reconcile), the reaper and the start-up orphan pass. They run on
different threads (the scheduler's worker, paho's network thread, the request
threadpool), and the task store has no locking of its own.

The discipline is the run store's existing per-run lock: every occurrence
transition below re-reads the occurrence under `run_store.lock(run_id)` and
writes only if it is still in flight. Every path that ENDS a run sets the run's
result under that same lock before touching the occurrence, so a writer holding
the lock that sees the run still "running" knows no ending has happened yet.
A settled occurrence is final: nothing here moves it again.
"""
from __future__ import annotations

from typing import Optional, Tuple

from runs.store import RunStore
from shared.schemas.runs import MissionRun
from shared.schemas.tasks import TaskOccurrence
from tasks.store import TaskStore

# An occurrence with a mission in flight -- what the scheduler counts as busy.
IN_FLIGHT = frozenset({"dispatched", "running"})


def outcome(run: MissionRun) -> Tuple[str, Optional[str]]:
    """The occurrence status and message for an ended run: completed is
    succeeded, an operator stop is cancelled, anything else failed."""
    if run.result == "completed":
        return "succeeded", run.end_reason
    if run.result == "aborted" and run.end_reason == "stopped":
        return "cancelled", run.end_reason
    return "failed", run.end_reason


def _put_if_in_flight(task_store: TaskStore, run_id: str, **update) -> Optional[TaskOccurrence]:
    """Caller holds the run's lock. Re-reads, and writes only an occurrence that
    is still in flight; returns the occurrence as it now stands."""
    current = task_store.get_occurrence(run_id)
    if current is None or current.status not in IN_FLIGHT:
        return current
    moved = current.model_copy(update=update)
    task_store.put_occurrence(moved)
    return moved


def settle(run_store: RunStore, task_store: TaskStore, run: MissionRun) -> Optional[TaskOccurrence]:
    """Give an in-flight occurrence its ended run's outcome."""
    status, message = outcome(run)
    with run_store.lock(run.run_id):
        return _put_if_in_flight(task_store, run.run_id, status=status, message=message)


def fail(run_store: RunStore, task_store: TaskStore, run_id: str,
         message: Optional[str]) -> Optional[TaskOccurrence]:
    """Fail an in-flight occurrence. Re-entrant: callers that already hold the
    run's lock (they just ended the run) may call it."""
    with run_store.lock(run_id):
        return _put_if_in_flight(task_store, run_id, status="failed", message=message)


def mark_running(run_store: RunStore, task_store: TaskStore, run_id: str,
                 message: Optional[str] = None) -> Optional[TaskOccurrence]:
    """The write after START_MISSION went out: "dispatched" becomes "running" --
    but only if the run has not already ended in the meantime (refused at once,
    or finished before this line). Then the occurrence takes the run's outcome
    instead, and an occurrence some other writer already settled stays as it is.
    Returns the occurrence as it now stands."""
    with run_store.lock(run_id):
        current = task_store.get_occurrence(run_id)
        if current is None or current.status != "dispatched":
            return current
        run = run_store.get(run_id)
        if run is not None and run.result != "running":
            status, why = outcome(run)
            return _put_if_in_flight(task_store, run_id, status=status, message=why)
        update = {"status": "running"}
        if message is not None:
            update["message"] = message
        return _put_if_in_flight(task_store, run_id, **update)
