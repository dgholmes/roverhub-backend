"""runs/dispatch.py and POST /robots/{id}/runs: starting a mission on the edge."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.commands import CommandStore
from realtime.mqtt_client import CommandPublishError
from robots.registry import RobotRegistry
from runs.dispatch import RunDispatcher
from runs.router import build_runs_router
from runs.store import RunStore
from shared.schemas.commands import Command
from shared.schemas.missions import CommandStep
from shared.schemas.runs import MissionRun
from shared.schemas.tasks import TaskOccurrence
from tasks.store import TaskStore

NOW = "2026-10-29T10:00:00Z"
RUN_ID = "run-m-1-rover-pi"
STEPS = [
    {"kind": "command", "blockId": "b1", "label": "Stand", "type": "SET_STATE",
     "params": {"target": "stand"}},
    {"kind": "if", "blockId": "b2", "label": "If", "condition": {"kind": "batteryBelow", "pct": 30},
     "then": [], "else": []},
]


def _mission(kind="program"):
    extra = {"program_state": '{"blocks":{}}'} if kind == "program" else {}
    return {"mission_id": "m-1", "name": "Patrol", "type": "patrol", "kind": kind, "creator": "x",
            "updated_at": "2026-10-23T00:00:00Z", "map_id": "map-1", "site_id": "site-a", **extra}


def _body(**over):
    return {"mission": _mission(), "steps": STEPS, "source": "editor",
            "started_by": "operator-dev", **over}


class Env:
    def __init__(self, tmp_path, fail_publish=False):
        self.registry = RobotRegistry()
        self.registry.upsert_from_registration("rover-pi", "site-a", "quad", "b-1", "0.0.11")
        self.registry.update_state("rover-pi", "STAND", "stand", bridge_boot_id="boot-a")
        self.runs = RunStore(str(tmp_path / "runs"))
        self.tasks = TaskStore(str(tmp_path / "tasks"))
        self.commands = CommandStore()
        self.published = []
        self.changes = []
        self.fail_publish = fail_publish
        self.dispatcher = RunDispatcher(
            self.registry, self.runs, self.tasks, self.commands, self._publish,
            lambda: NOW, lambda mission_id, robot_id: f"run-{mission_id}-{robot_id}",
            on_change=lambda run_id, path: self.changes.append((run_id, path)),
        )
        app = FastAPI()
        app.include_router(build_runs_router(str(tmp_path / "runs"), dispatcher=self.dispatcher))
        self.client = TestClient(app)

    def _publish(self, robot_id, command):
        if self.fail_publish:
            raise CommandPublishError("broker down")
        self.published.append((robot_id, command))

    def post(self, **over):
        return self.client.post("/robots/rover-pi/runs", json=_body(**over))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_start_creates_run_publishes_start_mission_with_aliased_steps(env):
    response = env.post(max_duration_s=600)

    assert response.status_code == 201
    run = response.json()["run"]
    assert (run["run_id"], run["result"], run["robot_id"], run["site_id"]) == (
        RUN_ID, "running", "rover-pi", "site-a")
    assert (run["source"], run["started_by"], run["bridge_boot_id"], run["started_at"]) == (
        "editor", "operator-dev", "boot-a", NOW)
    assert env.runs.get(RUN_ID).result == "running"
    assert env.changes == [(RUN_ID, None)]

    robot_id, command = env.published[0]
    assert robot_id == "rover-pi" and command.type == "START_MISSION"
    assert command.initiated_by == "operator-dev" and command.label == "Start Patrol"
    assert command.params["run_id"] == RUN_ID
    assert command.params["max_duration_s"] == 600
    # by_alias: IfStep's `else_` goes out as the wire name "else".
    assert "else" in command.params["steps"][1] and "else_" not in command.params["steps"][1]
    assert env.commands.get(command.command_id) is command
    assert response.json()["command"]["command_id"] == command.command_id


def _version(e, version):
    e.registry.upsert_from_registration("rover-pi", "site-a", "quad", "b-1", version)


def _state(e, sdk_state="stand", **kw):
    e.registry.update_state("rover-pi", "STAND", sdk_state, bridge_boot_id="boot-a", **kw)


@pytest.mark.parametrize("setup, phrase", [
    (lambda e: e.registry.mark_offline("rover-pi"), "rover-pi is offline"),
    (lambda e: _version(e, "0.0.10"), "runs schema 0.0.10, missions need 0.0.11"),
    (lambda e: _state(e, "passive"), "the robot is in E-Stop; release it first"),
    (lambda e: _state(e, active_run={"run_id": "x"}), "rover-pi is already running a mission"),
    (lambda e: _state(e, control_owner="operator-dev"),
     "someone has control of rover-pi; release control first"),
])
def test_refusals(env, setup, phrase):
    setup(env)
    response = env.post()
    assert response.status_code == 409
    assert phrase in response.json()["detail"]
    assert env.published == [] and env.runs.list() == []


def test_a_running_run_in_the_store_blocks_a_second_start(env):
    assert env.post().status_code == 201
    second = env.post()
    assert second.status_code == 409
    assert "rover-pi is already running a mission" in second.json()["detail"]
    assert len(env.published) == 1


def test_a_program_needs_steps_and_a_route_must_come_without_them(env):
    # A route is compiled by the backend (schema 0.0.16, tests/test_runs_route.py):
    # one sent with steps is a malformed request, and this robot (0.0.11) is too
    # old to walk one anyway.
    assert env.post(mission=_mission("route")).status_code == 422
    route_refused = env.post(mission=_mission("route"), steps=[])
    assert route_refused.status_code == 409 and "routes need 0.0.16" in route_refused.json()["detail"]
    assert env.post(steps=[]).status_code == 409
    assert env.published == []


def test_publish_failure_marks_run_failed_and_returns_503(tmp_path):
    env = Env(tmp_path, fail_publish=True)
    env.tasks.put_occurrence(TaskOccurrence(
        occurrence_id=RUN_ID, task_id="t", mission_id="m-1", site_id="site-a",
        robot_id="rover-pi", due_at=NOW, status="running", run_id=RUN_ID))

    response = env.post()

    assert response.status_code == 503 and "broker down" in response.json()["detail"]
    run = env.runs.get(RUN_ID)
    assert (run.result, run.end_reason, run.ended_at) == ("failed", "broker down", NOW)
    assert env.tasks.get_occurrence(RUN_ID).status == "failed"
    assert (RUN_ID, None) in env.changes


def test_start_mission_ack_failure_fails_the_run_with_bridge_reason(env):
    env.post()
    command = env.published[0][1].model_copy(update={
        "current_stage": "execution_failed",
        "failure_reason": "REJECTED_CONTROL_HELD: operator-dev holds control"})

    env.dispatcher.on_ack(command)

    run = env.runs.get(RUN_ID)
    assert run.result == "failed"
    assert run.end_reason == "REJECTED_CONTROL_HELD: operator-dev holds control"


def test_ack_failure_falls_back_to_stage_and_ignores_other_commands(env):
    env.post()
    command = env.published[0][1]
    other = command.model_copy(update={"type": "SET_STATE", "current_stage": "execution_failed"})
    env.dispatcher.on_ack(other)
    assert env.runs.get(RUN_ID).result == "running"
    env.dispatcher.on_ack(command.model_copy(update={"current_stage": "execution_started"}))
    assert env.runs.get(RUN_ID).result == "running"
    env.dispatcher.on_ack(command.model_copy(update={"current_stage": "timed_out"}))
    assert env.runs.get(RUN_ID).end_reason == "timed_out"


def test_start_failed_on_an_ended_run_is_a_no_op(env):
    env.post()
    run = env.runs.get(RUN_ID)
    run.result, run.ended_at, run.end_reason = "completed", "2026-10-29T10:05:00Z", None
    env.runs.put(run)
    env.changes.clear()

    env.dispatcher.start_failed(RUN_ID, "late")
    env.dispatcher.start_failed("no-such-run", "late")

    assert env.runs.get(RUN_ID).result == "completed"
    assert env.changes == []


def test_publish_start_is_usable_on_its_own_for_a_scheduled_run(env):
    run = MissionRun.model_validate({
        "run_id": "t-1-20261029T100000Z", "mission_id": "m-1", "mission": _mission(),
        "robot_id": "rover-pi", "site_id": "site-a", "started_at": NOW, "started_by": "scheduler",
        "source": "schedule"})
    steps = [CommandStep(kind="command", blockId="b", label="Stand", type="SET_STATE")]

    command = env.dispatcher.publish_start(run, steps, None)

    assert isinstance(command, Command) and command.params["max_duration_s"] is None
    assert env.published[0][1] is command and command.initiated_by == "scheduler"


def test_app_ack_callback_feeds_the_dispatcher(monkeypatch):
    import api.app as app_module
    seen = []
    monkeypatch.setattr(app_module, "_run_dispatcher", type("D", (), {"on_ack": lambda self, c: seen.append(c)})())
    command = Command(command_id="c-wire", robot_id="r", type="START_MISSION", label="x", initiated_by="o")

    app_module._on_ack(command)

    assert seen == [command] and app_module.command_store.get("c-wire") is command
    assert app_module.runs_router.dispatcher is not None


# -- final-review fixes: scheduled path, ack timeout, legacy runs, boot id ------

def _sched_run(run_id="t-1-20261029T100000Z", **over):
    return MissionRun.model_validate({
        "run_id": run_id, "mission_id": "m-1", "mission": _mission(),
        "robot_id": "rover-pi", "site_id": "site-a", "started_at": NOW, "started_by": "schedule",
        "source": "schedule", **over})


_SCHED_STEPS = [CommandStep(kind="command", blockId="b", label="Stand", type="SET_STATE")]


def test_scheduled_start_publishes_when_the_checks_pass(env):
    run = _sched_run()
    env.runs.put(run)  # the scheduler writes the run before it publishes

    command = env.dispatcher.start_scheduled(run, _SCHED_STEPS, 60)

    assert env.published[0][1] is command
    assert command.params["max_duration_s"] == 60


@pytest.mark.parametrize("setup, phrase", [
    (lambda e: e.registry.mark_offline("rover-pi"), "rover-pi is offline"),
    (lambda e: _version(e, "0.0.10"), "missions need 0.0.11"),
    (lambda e: _state(e, "passive"), "the robot is in E-Stop"),
    (lambda e: _state(e, active_run={"run_id": "other"}), "already running a mission"),
    (lambda e: _state(e, control_owner="operator-dev"), "someone has control"),
])
def test_scheduled_start_applies_the_same_checks_and_publishes_nothing(env, setup, phrase):
    from runs.dispatch import StartRefused
    run = _sched_run()
    env.runs.put(run)
    setup(env)

    with pytest.raises(StartRefused, match=phrase):
        env.dispatcher.start_scheduled(run, _SCHED_STEPS, None)
    assert env.published == []


def test_scheduled_start_does_not_count_its_own_run_as_already_running(env):
    run = _sched_run()
    env.runs.put(run)
    other = _sched_run("t-2-20261029T100000Z")
    env.runs.put(other)  # a different running bridge run on the robot blocks

    from runs.dispatch import StartRefused
    with pytest.raises(StartRefused, match="already running"):
        env.dispatcher.start_scheduled(run, _SCHED_STEPS, None)


def test_scheduled_and_interactive_starts_share_one_lock(env):
    assert env.dispatcher._start_lock is env.dispatcher._start_lock
    run = _sched_run()
    env.runs.put(run)
    held = []
    original = env.dispatcher._check

    def spy(*a, **k):
        held.append(env.dispatcher._start_lock.locked())
        return original(*a, **k)
    env.dispatcher._check = spy
    env.dispatcher.start_scheduled(run, _SCHED_STEPS, None)
    assert held == [True]


def test_legacy_browser_run_left_running_does_not_block_a_start(env):
    legacy = MissionRun.model_validate({
        "run_id": "old-browser-run", "mission_id": "m-1", "mission": _mission(),
        "robot_id": "rover-pi", "site_id": "site-a", "started_at": NOW, "started_by": "x"})
    env.runs.put(legacy)  # no source, no bridge_boot_id

    assert env.post().status_code == 201


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _timed(env):
    env.clock_s = _Clock()
    env.dispatcher._clock_s = env.clock_s
    return env.clock_s


def test_unanswered_start_fails_the_run_no_answer_after_30s(env):
    clock = _timed(env)
    env.post()
    env.tasks.put_occurrence(TaskOccurrence(
        occurrence_id=RUN_ID, task_id="t", mission_id="m-1", site_id="site-a",
        robot_id="rover-pi", due_at=NOW, status="running", run_id=RUN_ID))
    clock.t += 29
    env.dispatcher.sweep_unanswered()
    assert env.runs.get(RUN_ID).result == "running"

    clock.t += 2
    env.dispatcher.sweep_unanswered()

    run = env.runs.get(RUN_ID)
    assert (run.result, run.end_reason) == ("failed", "noAnswer")
    assert env.tasks.get_occurrence(RUN_ID).status == "failed"


def test_a_bridge_received_ack_stops_the_ack_timer(env):
    clock = _timed(env)
    env.post()
    command = env.published[0][1]
    env.dispatcher.on_ack(command.model_copy(update={"current_stage": "cloud_validated"}))
    env.dispatcher.on_ack(command.model_copy(update={"current_stage": "bridge_received"}))

    clock.t += 300
    env.dispatcher.sweep_unanswered()

    assert env.runs.get(RUN_ID).result == "running"


def test_cloud_stages_alone_do_not_count_as_an_answer(env):
    clock = _timed(env)
    env.post()
    command = env.published[0][1]
    env.dispatcher.on_ack(command.model_copy(update={"current_stage": "cloud_validated"}))
    clock.t += 31
    env.dispatcher.sweep_unanswered()
    assert env.runs.get(RUN_ID).end_reason == "noAnswer"


def test_noanswer_on_an_already_ended_run_is_a_no_op(env):
    clock = _timed(env)
    env.post()
    run = env.runs.get(RUN_ID)
    run.result, run.ended_at = "completed", NOW
    env.runs.put(run)
    clock.t += 60
    env.dispatcher.sweep_unanswered()
    assert env.runs.get(RUN_ID).result == "completed"


def test_ack_records_the_bridges_current_boot_id_on_the_run(env):
    env.post()  # boot-a
    env.registry.update_state("rover-pi", "STAND", "stand", bridge_boot_id="boot-b")
    command = env.published[0][1]

    env.dispatcher.on_ack(command.model_copy(update={"current_stage": "bridge_received"}))

    assert env.runs.get(RUN_ID).bridge_boot_id == "boot-b"
    assert (RUN_ID, None) in env.changes


# -- a robot the registry does not know (audit 2026-11-10) ---------------------

def test_starting_a_run_on_an_unknown_robot_is_404_and_nothing_is_published_or_stored(env):
    response = env.client.post("/robots/ghost/runs", json=_body())

    assert response.status_code == 404
    assert response.json() == {"detail": "robot not found"}
    assert env.published == []
    assert env.runs.list() == []


def test_starting_a_run_on_a_known_offline_robot_is_still_409_offline(env):
    env.registry.mark_offline("rover-pi")

    response = env.post()

    assert response.status_code == 409
    assert "offline" in response.json()["detail"]
    assert env.published == []
