"""api/app.py starts the scheduler loop and mounts the tasks router.

SchedulerLoop's logic is tested in test_scheduler_*.py; what only the app can get
wrong is whether the loop is actually started at startup, stopped at shutdown,
and built from the app's own registry and run store.
"""
import asyncio

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
