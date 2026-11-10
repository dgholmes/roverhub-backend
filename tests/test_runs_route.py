"""Starting a ROUTE mission (schema 0.0.16): the backend compiles it into steps
when the run starts, through every start path -- the runs endpoint (editor test
run), a task's run-now and the scheduler -- and refuses, in one sentence the
console shows verbatim, a route that cannot run.
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.commands import CommandStore
from missions.router import MissionStore
from robots.registry import RobotRegistry
from runs.dispatch import RunDispatcher
from runs.router import build_runs_router
from runs.store import RunStore
from shared.schemas.commands import command_ttl_s
from shared.schemas.missions import MissionLibraryEntry
from shared.schemas.tasks import MissionTask, TaskOccurrence
from tasks.router import build_tasks_router
from tasks.scheduler import EXECUTOR_KINDS, SchedulerLoop
from tasks.store import TaskStore
from tests.test_route_steps import route, site_map, wp

ROBOT = "rover-j"
NOW = "2026-11-11T10:00:00Z"


class Env:
    def __init__(self, tmp_path, schema="0.0.16", robot_type="quad", nav_map_id=None, smap="default"):
        self.registry = RobotRegistry(offline_after_s=None)
        self.registry.upsert_from_registration(ROBOT, "site-a", robot_type, "b-1", schema,
                                               nav_map_id=nav_map_id)
        self.registry.update_state(ROBOT, "STAND", "balance_stand", bridge_boot_id="boot-a")
        self.maps = {"map-office": site_map()} if smap == "default" else ({} if smap is None else {"map-office": smap})
        self.runs = RunStore(str(tmp_path / "runs"))
        self.tasks_dir = str(tmp_path / "tasks")
        self.tasks = TaskStore(self.tasks_dir)
        self.published = []
        self.on_publish = None
        self.dispatcher = RunDispatcher(
            self.registry, self.runs, self.tasks, CommandStore(), self._publish,
            clock_iso=lambda: NOW, new_run_id=lambda m, r: f"run-{m}-{r}",
            map_lookup=self.maps.get)
        self.missions = MissionStore(str(tmp_path / "missions"))
        app = FastAPI()
        app.include_router(build_runs_router(str(tmp_path / "runs"), dispatcher=self.dispatcher))
        app.include_router(build_tasks_router(self.tasks_dir, dispatcher=self.dispatcher,
                                              mission_store=self.missions, clock=lambda: NOW))
        self.client = TestClient(app)

    def _publish(self, robot_id, command):
        self.published.append(command)
        if self.on_publish is not None:
            self.on_publish(command)

    def start(self, mission=None, **body):
        mission = mission if mission is not None else route()
        payload = {"mission": mission.model_dump(mode="json"), "source": "editor",
                   "started_by": "operator-dev", **body}
        return self.client.post(f"/robots/{ROBOT}/runs", json=payload)

    def detail(self, response):
        return response.json()["detail"]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# -- compiled at start ---------------------------------------------------------------

def test_a_route_with_no_steps_is_compiled_and_started(env):
    response = env.start()

    assert response.status_code == 201, response.text
    (command,) = env.published
    assert command.type == "START_MISSION"
    sent = command.params["steps"]
    assert [s["kind"] for s in sent] == ["navigate", "navigate", "command", "wait", "command", "navigate"]
    assert sent[1]["waypointId"] == "wp-2" and sent[1]["goal"]["map_id"] == "map-office"
    # No cap asked for: the compiled route's own cap is sent.
    assert command.params["max_duration_s"] > 0
    assert command.ttl_s == command_ttl_s("START_MISSION") == 30.0
    run = env.runs.get(response.json()["run"]["run_id"])
    assert (run.result, run.mission.kind) == ("running", "route")


def test_a_cap_the_caller_set_is_kept(env):
    env.start(max_duration_s=120)
    assert env.published[0].params["max_duration_s"] == 120


def test_a_route_sent_with_steps_is_422(env):
    step = {"kind": "wait", "blockId": "w", "label": "w", "seconds": 1}
    response = env.start(steps=[step])
    assert response.status_code == 422
    assert env.detail(response) == (
        "a route is compiled into steps by the backend when it starts; send it with no steps")
    assert env.published == [] and env.runs.list() == []


# -- refusals, word for word -----------------------------------------------------------

def _refused(env, response, status=409):
    assert response.status_code == status, response.text
    assert env.published == [] and env.runs.list() == []
    return env.detail(response)


def test_a_bridge_older_than_0016_is_refused(tmp_path):
    env = Env(tmp_path, schema="0.0.15")
    assert _refused(env, env.start()) == "update the edge device: it runs schema 0.0.15, routes need 0.0.16"


def test_a_missing_map_is_refused(tmp_path):
    env = Env(tmp_path, smap=None)
    assert _refused(env, env.start()) == (
        "the map map-office this route is drawn on is not in the map store; "
        "upload it in Missions › Maps or draw the route on another map")


def test_a_draft_map_is_refused(tmp_path):
    env = Env(tmp_path, smap=site_map(status="draft"))
    assert _refused(env, env.start()) == "the map Office 3F is a draft; activate it in Missions › Maps"


def test_an_archived_map_is_refused(tmp_path):
    env = Env(tmp_path, smap=site_map(status="archived"))
    assert _refused(env, env.start()) == (
        "the map Office 3F is archived; make it active again in Missions › Maps")


def test_a_robot_on_another_map_is_refused(tmp_path):
    env = Env(tmp_path, nav_map_id="map-warehouse")
    assert _refused(env, env.start()) == (
        f"{ROBOT} is localised on the map map-warehouse, not on Office 3F, which this route is drawn on; "
        "load that map on the robot or pick a robot that has it")


def test_a_robot_on_the_same_map_or_not_saying_is_fine(tmp_path):
    for nav_map_id in ("map-office", None):
        env = Env(tmp_path / str(nav_map_id), nav_map_id=nav_map_id)
        assert env.start().status_code == 201


def test_a_wheel_robot_is_refused(tmp_path):
    env = Env(tmp_path, robot_type="wheel")
    assert _refused(env, env.start()) == (
        "routes run only on four-legged robots for now, and this robot is a wheel robot; "
        "pick a four-legged robot to run it")


def test_a_waypoint_in_a_no_go_zone_is_refused_before_anything_is_sent(env):
    mission = route([wp("wp-1", 5, 5, "Stairwell")])
    assert _refused(env, env.start(mission)) == (
        "waypoint Stairwell is in or within 0.4 m of the no-go zone stairs; "
        "move it further out in the mission editor")


def test_a_program_carrying_a_navigate_step_is_refused(env):
    nav = {"kind": "navigate", "blockId": "n", "label": "n", "waypointId": "w",
           "goal": {"x_m": 0, "y_m": 0, "map_id": "map-office", "floor_id": "0"}}
    nested = {"kind": "repeat", "blockId": "r", "label": "r", "times": 2, "body": [nav]}
    mission = MissionLibraryEntry(
        mission_id="m-prog", name="Prog", type="patrol", kind="program", creator="x",
        updated_at="t", map_id="map-office", site_id="site-a", program_state="{}")
    assert _refused(env, env.start(mission, steps=[nested])) == (
        "this program contains a walk-to-point step, which only a route can carry; "
        "remove it, or draw the walk as a route")


# -- a task's run-now -----------------------------------------------------------------

def _task(env, max_duration_min=None):
    env.missions.put(route())
    task = MissionTask(task_id="t-1", mission_id="m-route", site_id="site-a", robot_id=ROBOT,
                       max_duration_min=max_duration_min, created_by="x", created_at="t", updated_at="t")
    TaskStore(env.tasks_dir).put_task(task)
    return task


def test_run_now_starts_a_route_with_no_steps_in_the_body(env):
    _task(env)

    response = env.client.post("/tasks/t-1/run-now", json={"started_by": "operator-dev"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["occurrence"]["status"] == "running"
    assert [s["kind"] for s in env.published[0].params["steps"]][:2] == ["navigate", "navigate"]


def test_run_now_with_an_explicitly_empty_step_list_is_the_same(env):
    _task(env, max_duration_min=10)
    response = env.client.post("/tasks/t-1/run-now", json={"steps": [], "started_by": "operator-dev"})
    assert response.status_code == 201
    assert env.published[0].params["max_duration_s"] == 600


def test_a_route_the_bridge_refuses_at_once_settles_its_occurrence(env):
    _task(env)
    env.on_publish = lambda command: env.dispatcher.on_ack(command.model_copy(update={
        "current_stage": "execution_failed",
        "failure_reason": "REJECTED_NAV_MAP_MISMATCH: the point is on map 'map-office'"}))

    response = env.client.post("/tasks/t-1/run-now", json={"started_by": "operator-dev"})

    occ = TaskStore(env.tasks_dir).get_occurrence(response.json()["occurrence"]["occurrence_id"])
    assert occ.status == "failed" and occ.message.startswith("REJECTED_NAV_MAP_MISMATCH")
    assert env.runs.get(occ.run_id).result == "failed"


# -- the scheduler ---------------------------------------------------------------------

DUE = "2026-11-11T09:00:00Z"
AT_DUE = datetime(2026, 11, 11, 9, 0, 30, tzinfo=timezone.utc)


def test_route_is_an_executor_kind():
    assert "route" in EXECUTOR_KINDS


def _scheduled(env, unattended=True):
    task = _task(env)
    TaskStore(env.tasks_dir).put_occurrence(TaskOccurrence(
        occurrence_id="t-1-20261111T090000Z", task_id="t-1", mission_id="m-route",
        site_id="site-a", robot_id=ROBOT, due_at=DUE, status="due"))
    loop = SchedulerLoop(
        TaskStore(env.tasks_dir), env.registry, env.missions,
        # As api/app.py's _publish_scheduled does for a route: no steps, the
        # task's cap (None here), compiled by the dispatcher.
        lambda occ, run: env.dispatcher.start_scheduled(run, [], None),
        run_store=env.runs, unattended_enabled=unattended)
    return task, loop


def test_a_scheduled_route_is_compiled_and_dispatched_when_unattended_runs_are_on(env):
    _, loop = _scheduled(env)

    loop.run_once(AT_DUE)

    occ = TaskStore(env.tasks_dir).get_occurrence("t-1-20261111T090000Z")
    assert occ.status == "running"
    (command,) = env.published
    assert command.params["run_id"] == occ.run_id
    assert command.params["steps"][0]["kind"] == "navigate"
    assert command.params["max_duration_s"] > 0


def test_a_scheduled_route_stays_behind_the_unattended_switch(env):
    _, loop = _scheduled(env, unattended=False)
    loop.run_once(AT_DUE)
    occ = TaskStore(env.tasks_dir).get_occurrence("t-1-20261111T090000Z")
    assert (occ.status, occ.skip_reason) == ("skipped", "unattended_disabled")
    assert env.published == []


def test_a_scheduled_route_refused_by_the_bridge_at_once_frees_the_robot(env):
    _, loop = _scheduled(env)
    env.on_publish = lambda command: env.dispatcher.on_ack(command.model_copy(update={
        "current_stage": "execution_failed", "failure_reason": "REJECTED_ROBOT_IN_PASSIVE"}))

    loop.run_once(AT_DUE)

    occ = TaskStore(env.tasks_dir).get_occurrence("t-1-20261111T090000Z")
    assert (occ.status, occ.message) == ("failed", "REJECTED_ROBOT_IN_PASSIVE")
    assert env.runs.get(occ.run_id).result == "failed"


def test_a_scheduled_route_the_backend_refuses_fails_with_the_sentence(tmp_path):
    env = Env(tmp_path, smap=site_map(status="draft"))
    _, loop = _scheduled(env)
    loop.run_once(AT_DUE)
    occ = TaskStore(env.tasks_dir).get_occurrence("t-1-20261111T090000Z")
    assert occ.status == "failed"
    assert "the map Office 3F is a draft; activate it in Missions › Maps" in occ.message
    assert env.published == []
