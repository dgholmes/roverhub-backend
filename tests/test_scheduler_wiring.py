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


def test_the_publish_stub_says_what_is_missing():
    # Unreachable while EXECUTOR_KINDS is empty; when someone makes it reachable
    # the error must say why, not just that.
    with pytest.raises(NotImplementedError) as exc:
        app_module._publish_mission_start(None, None)
    message = str(exc.value)
    assert "CommandType" in message and "executor" in message


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
