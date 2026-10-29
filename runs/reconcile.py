"""Fail the runs a bridge restart lost (edge mission runs spec §6.7)."""
from __future__ import annotations

import logging
from typing import List, Optional

from runs.store import RunStore
from tasks.store import TaskStore

logger = logging.getLogger("roverhub.runs")


def reconcile_boot(
    run_store: RunStore, task_store: TaskStore, robot_id: str,
    boot_id: Optional[str], now_iso: str,
) -> List[str]:
    """A run still "running" under a boot id other than the one the bridge now
    reports died with the old process: nothing will ever send its run_ended.

    Each run is compared with its OWN recorded boot id, not with the registry's
    previous one -- after a backend restart there is no previous. A run with no
    boot id is a browser-driven run, which a bridge restart does not end, so it
    is left alone; so is an unknown new boot id (an old bridge)."""
    if boot_id is None:
        return []
    changed: List[str] = []
    for listed in run_store.list(robot_id=robot_id):
        # Re-read under the run's lock: the bridge's run_ended may land at the
        # same moment, and a run that just completed must not be overwritten.
        with run_store.lock(listed.run_id):
            run = run_store.get(listed.run_id)
            if run is None or run.result != "running":
                continue
            if run.bridge_boot_id is None or run.bridge_boot_id == boot_id:
                continue
            run.result = "failed"
            run.ended_at = now_iso
            run.end_reason = "bridgeRestarted"
            run_store.put(run)
        occurrence = task_store.get_occurrence(run.run_id)
        if occurrence is not None:
            occurrence.status = "failed"
            task_store.put_occurrence(occurrence)
        logger.warning("run %s failed: bridge on %s restarted (boot %s -> %s)",
                       run.run_id, robot_id, run.bridge_boot_id, boot_id)
        changed.append(run.run_id)
    return changed
