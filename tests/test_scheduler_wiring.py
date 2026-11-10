"""api/app.py starts the scheduler loop and mounts the tasks router.

SchedulerLoop's logic is tested in test_scheduler_*.py; what only the app can get
wrong is whether the loop is actually started at startup, stopped at shutdown,
and built from the app's own registry and run store.
"""
import asyncio
import logging

import pytest

from fastapi.testclient import TestClient

import api.app as app_module
from tasks.scheduler import EXECUTOR_KINDS, SchedulerLoop


class _FakeLoop:
    def __init__(self):
        self.running = False
        self.cancelled = False

    async def run(self):
        self.running = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _no_mqtt(monkeypatch):
    # connect() would dial a broker; the wiring under test is the loop's.
    monkeypatch.setattr(app_module.mqtt_client, "connect", lambda: None)
    monkeypatch.setattr(app_module.mqtt_client, "disconnect", lambda: None)


def test_startup_runs_the_loop_and_shutdown_cancels_it(monkeypatch):
    fake = _FakeLoop()
    monkeypatch.setattr(app_module, "scheduler_loop", fake)
    _no_mqtt(monkeypatch)

    with TestClient(app_module.app) as client:
        assert client.get("/healthz").status_code == 200
        assert fake.running, "startup did not start the scheduler loop"
        assert not fake.cancelled
    assert fake.cancelled, "shutdown did not cancel the scheduler loop"


def test_the_kill_switch_starts_no_task_and_logs_that(monkeypatch, caplog):
    fake = _FakeLoop()
    monkeypatch.setattr(app_module, "scheduler_loop", fake)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    _no_mqtt(monkeypatch)

    with caplog.at_level(logging.INFO, logger="roverhub.tasks"):
        with TestClient(app_module.app) as client:
            assert client.get("/healthz").status_code == 200  # lets a created task run
            assert not fake.running
            # Inside the block: shutdown clears the handle either way, so after
            # the block this would hold even if startup had created a task.
            assert getattr(app_module.app.state, "scheduler_task", None) is None
    assert "scheduler loop disabled" in caplog.text


@pytest.mark.parametrize("value", [None, "", "true", "1", "yes", "on", "anything"])
def test_the_loop_defaults_to_on_and_logs_that(monkeypatch, caplog, value):
    fake = _FakeLoop()
    monkeypatch.setattr(app_module, "scheduler_loop", fake)
    if value is None:
        monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("SCHEDULER_ENABLED", value)
    _no_mqtt(monkeypatch)

    with caplog.at_level(logging.INFO, logger="roverhub.tasks"):
        with TestClient(app_module.app) as client:
            assert client.get("/healthz").status_code == 200  # lets the task take its first step
            assert fake.running
    assert "scheduler loop started" in caplog.text


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " Off "])
def test_every_off_spelling_disables_it(monkeypatch, value):
    fake = _FakeLoop()
    monkeypatch.setattr(app_module, "scheduler_loop", fake)
    monkeypatch.setenv("SCHEDULER_ENABLED", value)
    _no_mqtt(monkeypatch)
    with TestClient(app_module.app) as client:
        # A request gives a task the switch wrongly created time to start, so a
        # spelling that is ignored fails here instead of passing by being early.
        assert client.get("/healthz").status_code == 200
        assert not fake.running


def test_the_loop_is_built_from_the_apps_own_collaborators():
    loop = app_module.scheduler_loop
    assert isinstance(loop, SchedulerLoop)
    assert loop._registry is app_module.registry
    assert loop._runs is app_module.runs_router.store
    # Nothing can execute a mission yet, so this must stay the empty set rather
    # than a hard-coded kind: the calendar shows honest no_executor skips.
    assert loop._executor_kinds is EXECUTOR_KINDS


def test_the_tasks_router_is_mounted():
    body = TestClient(app_module.app).get("/tasks").json()
    assert isinstance(body["items"], list)


def _program_mission(quad=None, wheel=None, version=None):
    from shared.schemas.missions import (
        PROGRAM_COMPILER_VERSION, MissionLibraryEntry, ProgramSteps,
    )
    steps = None
    if quad is not None or wheel is not None:
        steps = ProgramSteps(
            compiler_version=PROGRAM_COMPILER_VERSION if version is None else version,
            quad=quad or [], wheel=wheel or [])
    return MissionLibraryEntry(
        mission_id="m-1", name="Patrol", type="patrol", kind="program", creator="x",
        updated_at="2026-10-23T00:00:00Z", map_id="map-1", site_id="site-a",
        program_state='{"blocks":{}}', program_steps=steps)


def _cmd(type_="SNAPSHOT", speech=None):
    from shared.schemas.missions import CommandStep, SpeechSpec
    return CommandStep(kind="command", blockId="b", label="x", type=type_,
                       speech=SpeechSpec(text="hi", untilDone=True) if speech else None)


class _Dispatcher:
    def __init__(self):
        self.calls = []

    def start_scheduled(self, run, steps, max_duration_s):
        self.calls.append((run, steps, max_duration_s))


def _scheduled_world(monkeypatch, mission, robot_type="quad", max_duration_min=None):
    from robots.registry import RobotRecord
    from shared.schemas.runs import MissionRun
    from shared.schemas.tasks import MissionTask, TaskOccurrence

    dispatcher = _Dispatcher()
    monkeypatch.setattr(app_module, "_run_dispatcher", dispatcher)
    monkeypatch.setattr(app_module, "_scheduler_missions", type("S", (), {"get": lambda self, i: mission})())
    monkeypatch.setattr(app_module.registry, "get", lambda rid: RobotRecord(
        robot_id="robot-1", site_id="site-a", robot_type=robot_type, name="R", online=True))
    task = MissionTask(
        task_id="t-1", mission_id="m-1", site_id="site-a", robot_id="robot-1",
        max_duration_min=max_duration_min, created_by="x",
        created_at="2026-10-23T00:00:00Z", updated_at="2026-10-23T00:00:00Z")
    monkeypatch.setattr(app_module._reconcile_task_store, "get_task", lambda tid: task)
    occ = TaskOccurrence(occurrence_id="t-1-x", task_id="t-1", mission_id="m-1",
                         site_id="site-a", robot_id="robot-1", due_at="2026-10-24T09:00:00Z")
    run = MissionRun(run_id="t-1-x", mission_id="m-1", mission=mission,
                     robot_id="robot-1", site_id="site-a", started_at="2026-10-24T09:00:00Z",
                     started_by="schedule")
    return dispatcher, occ, run


def test_scheduled_publish_sends_the_robot_types_steps_and_the_task_cap(monkeypatch):
    mission = _program_mission(quad=[_cmd("SNAPSHOT")], wheel=[_cmd("CHANGE_MODE")])
    dispatcher, occ, run = _scheduled_world(monkeypatch, mission, "wheel", max_duration_min=2)
    app_module._publish_scheduled(occ, run)
    (_, steps, cap), = dispatcher.calls
    assert [s.type for s in steps] == ["CHANGE_MODE"] and cap == 120


def test_scheduled_publish_without_a_cap_passes_none(monkeypatch):
    dispatcher, occ, run = _scheduled_world(monkeypatch, _program_mission(quad=[_cmd()]))
    app_module._publish_scheduled(occ, run)
    assert dispatcher.calls[0][2] is None


@pytest.mark.parametrize("mission_kwargs", [
    {},                                        # never compiled
    {"quad": [_cmd()], "version": 0},          # stale compiler
    {"wheel": [_cmd()]},                       # nothing for a quad
])
def test_scheduled_publish_refuses_uncompiled_or_stale_steps(monkeypatch, mission_kwargs):
    dispatcher, occ, run = _scheduled_world(monkeypatch, _program_mission(**mission_kwargs))
    with pytest.raises(RuntimeError, match="open and save this mission once"):
        app_module._publish_scheduled(occ, run)
    assert dispatcher.calls == []


def test_scheduled_publish_refuses_an_unknown_robot_type(monkeypatch):
    dispatcher, occ, run = _scheduled_world(monkeypatch, _program_mission(quad=[_cmd()]), robot_type="hexapod")
    with pytest.raises(RuntimeError, match="open and save this mission once"):
        app_module._publish_scheduled(occ, run)


def test_scheduled_publish_refuses_speech(monkeypatch):
    from shared.schemas.missions import RepeatStep
    nested = RepeatStep(kind="repeat", blockId="r", label="r", times=2,
                        body=[_cmd(), _cmd("SPEAK")])
    dispatcher, occ, run = _scheduled_world(monkeypatch, _program_mission(quad=[nested]))
    with pytest.raises(RuntimeError, match="scheduled runs cannot play speech yet"):
        app_module._publish_scheduled(occ, run)
    assert dispatcher.calls == []


def test_has_spoken_steps_finds_speech_in_every_nested_body():
    from shared.schemas.missions import Condition, IfStep, RepeatUntilStep
    from tasks.scheduler import has_spoken_steps
    cond = Condition(kind="fallen")
    spoken = _cmd("SNAPSHOT", speech=True)
    assert not has_spoken_steps([_cmd()])
    assert has_spoken_steps([spoken])
    assert has_spoken_steps([RepeatUntilStep(kind="repeatUntil", blockId="a", label="a", condition=cond, body=[spoken])])
    assert has_spoken_steps([IfStep(kind="if", blockId="a", label="a", condition=cond, then=[_cmd()], **{"else": [spoken]})])
    deep = IfStep(kind="if", blockId="a", label="a", condition=cond, then=[
        RepeatUntilStep(kind="repeatUntil", blockId="b", label="b", condition=cond, body=[_cmd("SPEAK")])], **{"else": []})
    assert has_spoken_steps([deep])


@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("false", False), ("0", False), ("true", True), ("1", True), ("ON", True),
])
def test_app_reads_unattended_runs_enabled_default_false(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("UNATTENDED_RUNS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("UNATTENDED_RUNS_ENABLED", value)
    assert app_module._unattended_runs_enabled() is expected


def test_the_app_loop_is_built_with_unattended_off_by_default():
    assert app_module.scheduler_loop._unattended_enabled is False


def test_shutdown_still_disconnects_mqtt_when_the_loop_task_raised(monkeypatch):
    class _Crashes(_FakeLoop):
        async def run(self):
            self.running = True
            raise RuntimeError("tick blew up")

    disconnected = []
    monkeypatch.setattr(app_module, "scheduler_loop", _Crashes())
    monkeypatch.setattr(app_module.mqtt_client, "connect", lambda: None)
    monkeypatch.setattr(app_module.mqtt_client, "disconnect", lambda: disconnected.append(True))

    with TestClient(app_module.app) as client:
        assert client.get("/healthz").status_code == 200
    assert disconnected == [True]


def test_startup_runs_the_run_watchdog_and_shutdown_stops_it(monkeypatch):
    import time
    sweeps = []
    monkeypatch.setattr(app_module, "_sweep_runs", lambda: sweeps.append(1))
    monkeypatch.setattr(app_module, "_RUN_WATCH_INTERVAL_S", 0.01)
    monkeypatch.setattr(app_module, "scheduler_loop", _FakeLoop())
    _no_mqtt(monkeypatch)

    with TestClient(app_module.app):
        deadline = time.time() + 2
        while len(sweeps) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(sweeps) >= 2
        task = app_module.app.state.run_watch_task
    assert task.cancelled() or task.done()
    assert app_module.app.state.run_watch_task is None


def test_the_reaper_closes_runs_through_the_dispatcher():
    assert app_module.run_reaper._fail == app_module._run_dispatcher.start_failed


def test_startup_settles_orphans_before_the_scheduler_or_mqtt_starts(monkeypatch):
    # Before the first tick, or a robot wedged by the last process is skipped as
    # robot_busy once more; before MQTT, so the snapshot is the old process's.
    order = []

    class _Settler:
        def start(self):
            order.append("settle")

        def note_state(self, robot_id):
            pass

        def sweep(self):
            pass

    class _Loop(_FakeLoop):
        async def run(self):
            order.append("scheduler")
            await super().run()

    monkeypatch.setattr(app_module, "orphan_settler", _Settler())
    monkeypatch.setattr(app_module, "scheduler_loop", _Loop())
    monkeypatch.setattr(app_module.mqtt_client, "connect", lambda: order.append("mqtt"))
    monkeypatch.setattr(app_module.mqtt_client, "disconnect", lambda: None)

    with TestClient(app_module.app) as client:
        assert client.get("/healthz").status_code == 200
    assert order[0] == "settle" and set(order) == {"settle", "mqtt", "scheduler"}


def test_the_settler_closes_runs_through_the_dispatcher_and_hears_every_state(monkeypatch):
    assert app_module.orphan_settler._fail == app_module._run_dispatcher.start_failed
    noted, swept = [], []
    monkeypatch.setattr(app_module.orphan_settler, "note_state", noted.append)
    monkeypatch.setattr(app_module.orphan_settler, "sweep", lambda: swept.append(1))
    monkeypatch.setattr(app_module, "_reconcile_hook", None)

    app_module._on_state("rvr-01", None, "boot-a")
    app_module._sweep_runs()

    assert noted == ["rvr-01"] and swept == [1]


def _route_mission():
    from tests.test_route_steps import route
    return route()


def test_scheduled_publish_of_a_route_sends_no_steps_and_the_task_cap(monkeypatch):
    # The dispatcher compiles a route; the app neither looks for program steps
    # nor refuses its pre-made announcement clips as speech.
    dispatcher, occ, run = _scheduled_world(monkeypatch, _route_mission(), max_duration_min=3)
    app_module._publish_scheduled(occ, run)
    (_, steps, cap), = dispatcher.calls
    assert steps == [] and cap == 180


def test_a_spoken_step_with_a_clip_already_made_is_not_speech_to_synthesise():
    from shared.schemas.missions import CommandStep, SpeechSpec
    from tasks.scheduler import has_spoken_steps
    made = CommandStep(kind="command", blockId="b", label="x", type="SPEAK", params={"clip_id": "c.wav"})
    typed = CommandStep(kind="command", blockId="b", label="x", type="SPEAK", params={"clip_id": "c.wav"},
                        speech=SpeechSpec(text="hi", untilDone=False))
    assert not has_spoken_steps([made])
    assert not has_spoken_steps([typed])


def test_the_dispatcher_reads_maps_from_the_maps_routers_store():
    assert app_module._run_dispatcher._map_lookup == app_module.maps_router.store.get
