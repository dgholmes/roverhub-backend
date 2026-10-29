"""Start a mission run on the edge (edge mission runs spec 6.6).

The dispatcher creates the MissionRun record and publishes START_MISSION; the
bridge's executor does the rest and reports back through the run topic. Refusals
are decided here, from what the registry last heard, so an operator gets a
reason in words instead of a command that fails on the robot."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from control.commands import CommandStore
from realtime.mqtt_client import CommandPublishError, _parse_version
from robots.registry import RobotRegistry
from runs.store import RunStore
from shared.schemas.commands import Command, CommandStageEvent
from shared.schemas.missions import MissionLibraryEntry, ProgramStep
from shared.schemas.runs import MissionRun, RunSource
from tasks.store import TaskStore

logger = logging.getLogger("roverhub.runs")

# The first schema in which the edge understands START_MISSION.
MISSION_SCHEMA_VERSION = "0.0.11"

OnChange = Callable[[str, Optional[str]], None]


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

    def _changed(self, run_id: str) -> None:
        if self._on_change is not None:
            self._on_change(run_id, None)

    def _check(self, robot_id: str, mission: MissionLibraryEntry, steps: List[ProgramStep]):
        """Raises StartRefused; returns the robot record when it can take a run."""
        robot = self._registry.get(robot_id)
        if robot is None or not robot.online:
            raise StartRefused(f"{robot_id} is offline")
        version = _parse_version(robot.schema_version)
        if version is None or version < _parse_version(MISSION_SCHEMA_VERSION):
            raise StartRefused(
                f"update the edge device: it runs schema {robot.schema_version or 'unknown'}, "
                f"missions need {MISSION_SCHEMA_VERSION}")
        if robot.sdk_state == "passive":
            raise StartRefused("the robot is in E-Stop; release it first")
        if robot.active_run or any(r.result == "running" for r in self._runs.list(robot_id=robot_id)):
            raise StartRefused(f"{robot_id} is already running a mission")
        if robot.control_owner:
            raise StartRefused(f"someone has control of {robot_id}; release control first")
        if mission.kind != "program":
            raise StartRefused("only program missions can run on a robot")
        if not steps:
            raise StartRefused("the program has no steps to run")
        return robot

    def start(
        self, robot_id: str, mission: MissionLibraryEntry, steps: List[ProgramStep], *,
        source: RunSource, started_by: str, max_duration_s: Optional[float] = None,
        run_id: Optional[str] = None,
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
            command = self.publish_start(run, steps, max_duration_s)
        return run, command

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
            raise StartRefused(str(exc), 503)
        return command

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
        occurrence = self._tasks.get_occurrence(run_id)
        if occurrence is not None:
            occurrence.status = "failed"
            self._tasks.put_occurrence(occurrence)
        self._changed(run_id)
        logger.warning("run %s failed to start: %s", run_id, reason)

    def on_ack(self, command: Command) -> None:
        """Ack hook: a START_MISSION the bridge refused or never answered."""
        if command.type != "START_MISSION":
            return
        if command.current_stage not in ("execution_failed", "timed_out"):
            return
        run_id = (command.params or {}).get("run_id")
        if run_id:
            self.start_failed(run_id, command.failure_reason or command.current_stage)


def make_run_ended_hook(task_store: TaskStore) -> Callable[[MissionRun], None]:
    """The occurrence shares its run's id, so a finished run settles its
    occurrence: completed is succeeded, an operator stop is cancelled, anything
    else failed. A run with no occurrence (the editor's) is nothing to do."""
    def on_run_ended(run: MissionRun) -> None:
        occurrence = task_store.get_occurrence(run.run_id)
        if occurrence is None:
            return
        # A settled occurrence is final: a resent run_ended (or one racing
        # start_failed) must not rewrite what an operator already saw.
        if occurrence.status in ("succeeded", "failed", "cancelled", "skipped"):
            return
        if run.result == "completed":
            occurrence.status = "succeeded"
        elif run.result == "aborted" and run.end_reason == "stopped":
            occurrence.status = "cancelled"
        else:
            occurrence.status = "failed"
        occurrence.message = run.end_reason
        task_store.put_occurrence(occurrence)
    return on_run_ended
