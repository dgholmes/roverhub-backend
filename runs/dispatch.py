"""Start a mission run on the edge (edge mission runs spec 6.6).

The dispatcher creates the MissionRun record and publishes START_MISSION; the
bridge's executor does the rest and reports back through the run topic. Refusals
are decided here, from what the registry last heard, so an operator gets a
reason in words instead of a command that fails on the robot."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from control.commands import CommandStore
from realtime.mqtt_client import CommandPublishError, UnknownRobotError, _parse_version
from robots.registry import RobotRegistry
from runs import occurrences
from runs.store import RunStore
from shared.schemas.commands import Command, CommandStageEvent
from shared.schemas.missions import MissionLibraryEntry, ProgramStep
from shared.schemas.runs import MissionRun, RunSource
from tasks.store import TaskStore

logger = logging.getLogger("roverhub.runs")

# The first schema in which the edge understands START_MISSION.
MISSION_SCHEMA_VERSION = "0.0.11"

OnChange = Callable[[str, Optional[str]], None]

# A START_MISSION no bridge has acknowledged within this long is lost: the
# bridge is down or never got it, and nothing else would ever close the run.
ACK_TIMEOUT_S = 30.0
# Ack stages the cloud itself writes; only a stage past these proves a bridge heard.
_CLOUD_STAGES = ("requested", "cloud_received", "cloud_validated")


class StartRefused(Exception):
    """The run was not started; `message` is operator-readable."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_run_id(mission_id: str, robot_id: str) -> str:
    """The format of the existing record: <UTC stamp>-<mission>-<robot>."""
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{mission_id}-{robot_id}"


class RunDispatcher:
    def __init__(
        self, registry: RobotRegistry, run_store: RunStore, task_store: TaskStore,
        command_store: CommandStore, publish_command: Callable[[str, Command], None],
        clock_iso: Callable[[], str] = _utc_iso,
        new_run_id: Callable[[str, str], str] = default_run_id,
        on_change: Optional[OnChange] = None,
        clock_s: Callable[[], float] = time.time,
    ):
        self._registry = registry
        self._runs = run_store
        self._tasks = task_store
        self._commands = command_store
        self._publish = publish_command
        self._clock = clock_iso
        self._new_run_id = new_run_id
        # The bucket copier's hook, same shape PUT /runs uses.
        self._on_change = on_change
        # Run ids differ, so the store's per-run locks cannot make "no run
        # running on this robot, then create one" atomic. This does.
        self._start_lock = threading.Lock()
        # Epoch seconds, injectable. run_id -> when its START_MISSION went out,
        # for the runs no bridge has answered yet.
        self._clock_s = clock_s
        self._unanswered: Dict[str, float] = {}

    def _changed(self, run_id: str) -> None:
        if self._on_change is not None:
            self._on_change(run_id, None)

    def _check(
        self, robot_id: str, mission: MissionLibraryEntry, steps: List[ProgramStep],
        exclude_run_id: Optional[str] = None,
    ):
        """Raises StartRefused; returns the robot record when it can take a run.
        exclude_run_id is a run that already exists for this very start (the
        scheduler writes its run before publishing), so it is not "another"."""
        robot = self._registry.get(robot_id)
        if robot is None:
            # Unknown is not offline: there is no topic to send it to at all
            # (realtime/mqtt_client.py), and the API's answer for a robot it
            # does not know is a 404 (robots/router.py).
            raise StartRefused("robot not found", 404)
        if not robot.online:
            raise StartRefused(f"{robot_id} is offline")
        version = _parse_version(robot.schema_version)
        if version is None or version < _parse_version(MISSION_SCHEMA_VERSION):
            raise StartRefused(
                f"update the edge device: it runs schema {robot.schema_version or 'unknown'}, "
                f"missions need {MISSION_SCHEMA_VERSION}")
        if robot.sdk_state == "passive":
            raise StartRefused("the robot is in E-Stop; release it first")
        if self._is_running(robot, robot_id, exclude_run_id):
            raise StartRefused(f"{robot_id} is already running a mission")
        if robot.control_owner:
            raise StartRefused(f"someone has control of {robot_id}; release control first")
        if mission.kind != "program":
            raise StartRefused("only program missions can run on a robot")
        if not steps:
            raise StartRefused("the program has no steps to run")
        return robot

    def _is_running(self, robot, robot_id: str, exclude_run_id: Optional[str]) -> bool:
        # registry.active_run is the live truth for bridge runs. A stored run
        # counts only if a bridge could close it (it has a source or a boot id):
        # a legacy browser run left "running" would otherwise block the robot forever.
        if robot.active_run and robot.active_run.get("run_id") != exclude_run_id:
            return True
        return any(
            r.result == "running" and r.run_id != exclude_run_id
            and (r.source is not None or r.bridge_boot_id is not None)
            for r in self._runs.list(robot_id=robot_id))

    def start(
        self, robot_id: str, mission: MissionLibraryEntry, steps: List[ProgramStep], *,
        source: RunSource, started_by: str, max_duration_s: Optional[float] = None,
        run_id: Optional[str] = None,
        before_publish: Optional[Callable[[MissionRun], None]] = None,
    ) -> Tuple[MissionRun, Command]:
        with self._start_lock:
            robot = self._check(robot_id, mission, steps)
            run_id = run_id or self._new_run_id(mission.mission_id, robot_id)
            if self._runs.get(run_id) is not None:
                raise StartRefused(f"run {run_id} already exists")
            run = MissionRun(
                run_id=run_id, mission_id=mission.mission_id, mission=mission,
                robot_id=robot_id, site_id=mission.site_id or robot.site_id,
                started_at=self._clock(), result="running", started_by=started_by,
                source=source, bridge_boot_id=robot.bridge_boot_id,
            )
            with self._runs.lock(run_id):
                self._runs.put(run)
            self._changed(run_id)
            # Still inside the start lock: a publish failure is settled (run
            # failed) before the next start looks at this robot.
            if before_publish is not None:
                before_publish(run)
            command = self.publish_start(run, steps, max_duration_s)
        return run, command

    def start_scheduled(
        self, run: MissionRun, steps: List[ProgramStep], max_duration_s: Optional[float],
    ) -> Command:
        """The scheduler's start: the run record already exists, but the same
        refusals and the same lock apply as for an operator's start. Raises
        StartRefused, which the scheduler loop records as the occurrence's failure."""
        with self._start_lock:
            self._check(run.robot_id, run.mission, steps, exclude_run_id=run.run_id)
            return self.publish_start(run, steps, max_duration_s)

    def publish_start(
        self, run: MissionRun, steps: List[ProgramStep], max_duration_s: Optional[float],
    ) -> Command:
        """Build and publish START_MISSION for an already-created run. The
        scheduler calls this directly, so it depends on nothing but the run."""
        now = datetime.now(timezone.utc).isoformat()
        command = Command(
            command_id=str(uuid.uuid4()), robot_id=run.robot_id, type="START_MISSION",
            label=f"Start {run.mission.name}",
            params={
                "run_id": run.run_id,
                "steps": [s.model_dump(by_alias=True) for s in steps],
                "max_duration_s": max_duration_s,
            },
            current_stage="requested", stages=[CommandStageEvent(stage="requested", at=now)],
            initiated_by=run.started_by,
        )
        self._commands.add(command)
        try:
            self._publish(run.robot_id, command)
        except CommandPublishError as exc:
            # Never reached the broker, so no bridge will ack it: settle both
            # the command and the run now instead of leaving them "running".
            command.current_stage = "execution_failed"
            command.failure_reason = str(exc)
            command.stages.append(CommandStageEvent(
                stage="execution_failed", at=datetime.now(timezone.utc).isoformat()))
            self._commands.add(command)
            self.start_failed(run.run_id, str(exc))
            # Unknown here means forgotten since _check: refused, not unavailable.
            status = 404 if isinstance(exc, UnknownRobotError) else 503
            raise StartRefused(str(exc), status) from exc
        self._unanswered[run.run_id] = self._clock_s()
        return command

    def sweep_unanswered(self) -> None:
        """Fail the runs whose START_MISSION no bridge acknowledged in time."""
        now = self._clock_s()
        for run_id, sent_at in list(self._unanswered.items()):
            if now - sent_at >= ACK_TIMEOUT_S:
                self._unanswered.pop(run_id, None)
                self.start_failed(run_id, "noAnswer")

    def start_failed(self, run_id: str, reason: str) -> None:
        """The edge never began the run. A run that already ended (its run_ended
        beat this ack, say) is left exactly as the bridge reported it."""
        with self._runs.lock(run_id):
            run = self._runs.get(run_id)
            if run is None or run.result != "running":
                return
            run.result = "failed"
            run.ended_at = self._clock()
            run.end_reason = reason
            self._runs.put(run)
            # Still under the run's lock, and only if still in flight: a settled
            # occurrence is final (runs/occurrences.py).
            occurrences.fail(self._runs, self._tasks, run_id, reason)
        self._changed(run_id)
        logger.warning("run %s failed to start: %s", run_id, reason)

    def mark_running(self, run_id: str, message: Optional[str] = None):
        """After START_MISSION went out: the run's occurrence becomes running
        unless the run already ended (runs/occurrences.mark_running)."""
        return occurrences.mark_running(self._runs, self._tasks, run_id, message)

    def on_ack(self, command: Command) -> None:
        """Ack hook: a START_MISSION the bridge refused or never answered."""
        if command.type != "START_MISSION":
            return
        run_id = (command.params or {}).get("run_id")
        if not run_id:
            return
        if command.current_stage in ("execution_failed", "timed_out"):
            self._unanswered.pop(run_id, None)
            self.start_failed(run_id, command.failure_reason or command.current_stage)
        elif command.current_stage not in _CLOUD_STAGES:
            self._unanswered.pop(run_id, None)
            self._adopt_boot_id(run_id, command.robot_id)

    def _adopt_boot_id(self, run_id: str, robot_id: str) -> None:
        """A bridge has the run, so the boot id it reports now is the one that
        runs it -- the id copied at creation may be the previous process's, if
        the start came just after a restart and before the new /state."""
        robot = self._registry.get(robot_id)
        if robot is None or robot.bridge_boot_id is None:
            return
        with self._runs.lock(run_id):
            run = self._runs.get(run_id)
            if run is None or run.result != "running" or run.bridge_boot_id == robot.bridge_boot_id:
                return
            run.bridge_boot_id = robot.bridge_boot_id
            self._runs.put(run)
        self._changed(run_id)


def make_run_ended_hook(
    task_store: TaskStore, run_store: Optional[RunStore] = None,
) -> Callable[[MissionRun], None]:
    """The occurrence shares its run's id, so a finished run settles its
    occurrence: completed is succeeded, an operator stop is cancelled, anything
    else failed (runs/occurrences.outcome). A run with no occurrence (the
    editor's) is nothing to do. A settled occurrence is final: a resent
    run_ended (or one racing start_failed) must not rewrite what an operator
    already saw.

    run_store is the one whose per-run lock the other occurrence writers hold;
    the app passes it. Without it the write is unlocked, which is still safe for
    this writer alone: it only runs once the run has ended, and every other
    writer leaves an ended run's occurrence alone or gives it this same outcome."""
    def on_run_ended(run: MissionRun) -> None:
        if run_store is not None:
            occurrences.settle(run_store, task_store, run)
            return
        status, message = occurrences.outcome(run)
        occurrence = task_store.get_occurrence(run.run_id)
        if occurrence is not None and occurrence.status in occurrences.IN_FLIGHT:
            task_store.put_occurrence(occurrence.model_copy(update={"status": status, "message": message}))
    return on_run_ended
