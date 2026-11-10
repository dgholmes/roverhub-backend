"""Fail the runs a bridge restart lost (edge mission runs spec §6.7)."""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from runs import occurrences
from runs.store import RunStore
from tasks.store import TaskStore

logger = logging.getLogger("roverhub.runs")


def reconcile_boot(
    run_store: RunStore, task_store: TaskStore, robot_id: str,
    boot_id: Optional[str], now_iso: str,
    on_change: Optional[Callable[[str, Optional[str]], None]] = None,
    active_run_id: Optional[str] = None,
) -> List[str]:
    """A run still "running" under a boot id other than the one the bridge now
    reports died with the old process: nothing will ever send its run_ended.

    Each run is compared with its OWN recorded boot id, not with the registry's
    previous one -- after a backend restart there is no previous. A run with no
    boot id is a browser-driven run, which a bridge restart does not end, so it
    is left alone; so is an unknown new boot id (an old bridge).

    on_change is the bucket copier's hook (same shape PUT /runs uses): writing
    through the store directly would otherwise leave the bucket copy saying
    "running" forever. One bad run is logged and skipped -- there is no retry,
    the new boot id is already recorded -- so it cannot strand the others.

    active_run_id is the run the new process's own state names: it is running
    right now under the new boot (it started in the moment between the restart
    and the first /state, so it still carries the old boot id) and is skipped."""
    if boot_id is None:
        return []
    changed: List[str] = []
    for listed in run_store.list(robot_id=robot_id):
        try:
            if listed.run_id == active_run_id:
                continue
            if _fail_if_lost(run_store, task_store, listed.run_id, boot_id, now_iso):
                changed.append(listed.run_id)
                if on_change is not None:
                    on_change(listed.run_id, None)
        except Exception:
            logger.exception("could not reconcile run %s after bridge restart", listed.run_id)
    return changed


def _fail_if_lost(run_store, task_store, run_id: str, boot_id: str, now_iso: str) -> bool:
    # Re-read under the run's lock: the bridge's run_ended may land at the same
    # moment, and a run that just completed must not be overwritten.
    with run_store.lock(run_id):
        run = run_store.get(run_id)
        if run is None or run.result != "running":
            return False
        if run.bridge_boot_id is None or run.bridge_boot_id == boot_id:
            return False
        old_boot = run.bridge_boot_id
        run.result = "failed"
        run.ended_at = now_iso
        run.end_reason = "bridgeRestarted"
        run_store.put(run)
        # Under the same lock, and only if still in flight (runs/occurrences.py).
        occurrences.fail(run_store, task_store, run_id, "bridgeRestarted")
    logger.warning("run %s failed: bridge restarted (boot %s -> %s)", run_id, old_boot, boot_id)
    return True


def make_state_hook(run_store, task_store, clock: Callable[[], str], on_change=None, registry=None):
    """The (robot_id, previous_boot_id, boot_id) hook for BackendMqttClient.

    Only a CHANGED boot id reconciles (spec 6.7): the heartbeat republishes the
    same id, and a run that ended while the cloud was unreachable must not be
    failed as bridgeRestarted just because its run_ended has not arrived yet."""
    def hook(robot_id: str, previous_boot_id: Optional[str], boot_id: Optional[str]) -> None:
        if boot_id is None or previous_boot_id == boot_id:
            return
        # The registry already holds this very state message (it is updated
        # before the hook fires), so its active_run is the new process's answer.
        record = registry.get(robot_id) if registry is not None else None
        active = (record.active_run or {}).get("run_id") if record is not None else None
        reconcile_boot(run_store, task_store, robot_id, boot_id, clock(), on_change,
                       active_run_id=active)
    return hook
