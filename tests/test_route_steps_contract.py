"""The steps this backend compiles from a route must be steps the bridge runs.

missions/route_steps.py writes them; roverhub-edge/robot_bridge/program_runner.py
runs them on the robot, and safety_manager.py decides which command types a
mission may carry. The two sides were written apart, so this hands the real
compiler's output to the real runner, with fake effects in place of the robot.
A renamed field, a reason the runner maps differently, or an action type the
bridge forbids in missions fails here, at the desk, instead of on a robot.

Both bridge modules are loaded by path from the edge repo checked out beside
this one (CI checks it out as a sibling). With no edge repo the tests skip --
except under CI, where a skip would hide exactly the drift this exists to catch,
so they fail instead. The bridge's `robot_bridge/` goes on the END of sys.path
(for `state_machine` and `config`), so it can never shadow a backend package.

Written against docs/superpowers/specs/2026-11-11-route-missions-on-nav-design.md
("Bridge" and "Work split"): `RunnerEffects.navigate(step)` returns
`(outcome, reason, position)` with outcome "arrived" on success, and
`RunnerEffects.block(step, reason)` pauses the run as blocked.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from missions.route_steps import compile_route
from shared.schemas.missions import CommandStep, NavStep, ProgramStep
from tests.test_route_steps import route, site_map, wp

BRIDGE = Path(__file__).resolve().parents[2] / "roverhub-edge" / "robot_bridge"


def _load(name: str):
    path = BRIDGE / f"{name}.py"
    if not path.is_file():
        if os.environ.get("CI"):
            pytest.fail(f"{path} not found: CI must check out roverhub-edge beside roverhub-backend")
        pytest.skip(f"robot_bridge/{name}.py not found beside this repo")
    if str(BRIDGE) not in sys.path:
        sys.path.append(str(BRIDGE))
    key = f"bridge_{name}_under_test"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module          # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def _three_leg_route():
    return route([
        wp("wp-1", 0, 0, "Gate A"),
        wp("wp-2", 3, 0, "Lobby", heading_deg=90, actions=[
            {"type": "snapshot", "label": "Photo", "params": {"camera": "rear"}},
            {"type": "wait", "label": "Hold", "params": {"seconds": 2}},
            {"type": "voice_prompt", "label": "Hello", "params": {"clip_id": "clip-1.wav"}},
        ]),
        wp("wp-3", 3, 2, "Desk"),
    ])


def _steps():
    return compile_route(_three_leg_route(), site_map(), "quad").steps


class _Clock:
    """Time that only moves when something sleeps."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


class _Effects:
    """RunnerEffects with a robot that is standing, owned and connected.

    `legs` answers navigate() per call, in order; past its end every leg
    arrives. `on_block` is what the executor's block() would do to the gate."""

    def __init__(self, pr, gate, legs=(), on_block=None):
        self._pr = pr
        self._gate = gate
        self._legs = list(legs)
        self._on_block = on_block
        self.navigated = []
        self.sent = []
        self.blocked = []
        self.log_entries = []

    async def navigate(self, step):
        self.navigated.append(step.waypointId)
        if self._legs:
            return self._legs.pop(0)
        return ("arrived", None, None)

    def block(self, step, reason):
        self.blocked.append((step.waypointId, reason))
        if self._on_block is not None:
            self._on_block(self._gate)

    async def send(self, step):
        self.sent.append(step.type)
        return self._pr.SendResult("completed")

    def robot(self):
        values = dict(sdk_connected=True, abstract_state="AUTO_MISSION", sdk_state="walk",
                      posture="upright", battery_percent=80.0, obstacle_avoidance=False,
                      state_age_s=0.0)
        # Fields the bridge adds later default to None rather than breaking this.
        return self._pr.RobotView(**{f.name: values.get(f.name) for f in dataclasses.fields(self._pr.RobotView)})

    def owns_control(self):
        return True

    def estop_active(self):
        return False

    def log(self, entry):
        self.log_entries.append(entry)

    def highlight(self, block_id):
        pass

    def on_redo(self, step):
        pass


def _run(pr, legs=(), on_block=None):
    clock = _Clock()
    gate = pr.RunGate(clock, sleep=clock.sleep)
    fx = _Effects(pr, gate, legs, on_block)
    outcome = asyncio.run(pr.run_program(_steps(), fx, gate))
    return outcome, fx


# -- needs only this repo ----------------------------------------------------------

def test_the_compiled_steps_are_program_steps_on_the_wire():
    steps = _steps()
    wire = [s.model_dump(by_alias=True) for s in steps]
    assert TypeAdapter(list[ProgramStep]).validate_python(wire) == steps
    assert [s.waypointId for s in steps if isinstance(s, NavStep)] == ["wp-1", "wp-2", "wp-3"]


# -- against the bridge --------------------------------------------------------------

def test_no_compiled_action_is_a_type_the_bridge_forbids_in_missions():
    sm = _load("safety_manager")
    types = {s.type for s in _steps() if isinstance(s, CommandStep)}
    assert types == {"SNAPSHOT", "SPEAK"}
    assert types.isdisjoint(sm.MISSION_FORBIDDEN_STEP_TYPES)


def test_the_bridge_walks_every_leg_in_order_and_completes():
    pr = _load("program_runner")
    outcome, fx = _run(pr)
    assert outcome.result == "completed", outcome
    assert fx.navigated == ["wp-1", "wp-2", "wp-3"]
    assert fx.sent == ["SNAPSHOT", "SPEAK"]
    assert fx.blocked == []


def test_a_leg_that_stays_blocked_through_its_retries_pauses_the_run_as_blocked():
    pr = _load("program_runner")
    retries = _steps()[0].retries                      # 2: three tries in all
    aborted = [("failed", "nav2_aborted", None)] * (retries + 1)

    def stop_while_blocked(gate):
        # The executor pauses the gate; nobody resumes it, then the run is stopped.
        gate.pause()
        gate.stop("blocked")

    outcome, fx = _run(pr, legs=aborted, on_block=stop_while_blocked)

    assert fx.navigated == ["wp-1"] * (retries + 1)
    assert [w for w, _ in fx.blocked] == ["wp-1"]
    assert "nav2_aborted" in fx.blocked[0][1]
    assert outcome.result == "aborted" and outcome.reason == "blocked"


def test_a_robot_on_another_map_ends_the_run_as_nav_unavailable_without_retrying():
    pr = _load("program_runner")
    mismatch = [("failed", "REJECTED_NAV_MAP_MISMATCH: the point is on map 'map-office'", None)]

    outcome, fx = _run(pr, legs=mismatch)

    assert fx.navigated == ["wp-1"]
    assert fx.blocked == []
    assert (outcome.result, outcome.reason) == ("aborted", "navUnavailable")


# -- quick actions (schema 0.0.34) ---------------------------------------------------

def _quick_route():
    return route([
        wp("wp-1", 0, 0, "Gate A", actions=[
            {"type": "quick_action", "label": "Gongxi", "params": {"state": "gongxi"}},
            {"type": "quick_action", "label": "Wave", "params": {"state": "wave", "seconds": 3}},
            {"type": "quick_action", "label": "Groove", "params": {"state": "groove"}},
        ]),
        wp("wp-2", 3, 0, "Lobby"),
    ])


def _quick_steps():
    return compile_route(_quick_route(), site_map(), "quad").steps


def test_a_quick_action_is_a_set_state_the_bridge_lets_a_mission_carry():
    sm = _load("safety_manager")
    for step in _quick_steps():
        if isinstance(step, CommandStep):
            assert step.type == "SET_STATE"
            assert sm.mission_step_allowed(step.type)
            assert sm.mission_command_allowed(step.type)


def test_the_bridge_gate_approves_every_quick_action_state_from_the_running_mission(tmp_path, monkeypatch):
    from missions.route_steps import QUICK_ACTIONS
    monkeypatch.setenv("NAV_MAP_CHOICE_FILE", str(tmp_path / "nav_map.json"))
    monkeypatch.setenv("SITE_CHOICE_FILE", str(tmp_path / "site.json"))
    sm = _load("safety_manager")
    # The very BridgeConfig the gate imports (`robot_bridge/` is on sys.path now).
    config = sys.modules["config"].BridgeConfig(
        robot_ip="192.168.5.2:50051", cloud_mqtt_url="mqtt://localhost:1883",
        bridge_id="bridge-test", site_id="site-a", robot_id="robot-1",
        telemetry_poll_hz=10, heartbeat_interval_s=5.0,
        safety_max_speed_ratio=80, safety_min_battery_pct=15.0,
        stale_telemetry_threshold_s=3.0, cmd_timeout_s=10.0,
    )
    gate = sm.SafetyManager(config)
    owner = "mission:run-1"
    gate.acquire_control(owner)
    from missions.route_steps import QUAD_LEG_GAITS
    # Every move, every held move's stop, and every gait a leg may set (0.0.35),
    # including those ROUTE_GAITS holds back for now.
    states = set(QUICK_ACTIONS) | {m.stop for m in QUICK_ACTIONS.values() if m.stop} | set(QUAD_LEG_GAITS)
    assert {"walk", "flying_trot", "rl"} <= states
    for state in sorted(states):
        verdict = gate.check("SET_STATE", sdk_connected=True, battery_percent=80.0, robot_type="quad",
                             state=state, initiated_by=owner, sdk_state="walk", params={"state": state})
        assert verdict.approved, (state, verdict.rejection_code)


def test_the_bridge_runs_a_route_with_quick_actions_in_order():
    pr = _load("program_runner")
    clock = _Clock()
    gate = pr.RunGate(clock, sleep=clock.sleep)
    fx = _Effects(pr, gate)
    sent_params = []
    send = fx.send

    async def recording_send(step):
        sent_params.append(step.params.get("state"))
        return await send(step)

    fx.send = recording_send
    outcome = asyncio.run(pr.run_program(_quick_steps(), fx, gate))
    assert outcome.result == "completed", outcome
    assert fx.navigated == ["wp-1", "wp-2"]
    assert sent_params == ["gongxi", "wave", "wave", "groove"]
    # The wave was held for its 3 s.
    assert clock.t >= 3


def test_the_bridge_runs_a_route_whose_legs_change_gait_in_order(monkeypatch):
    import missions.route_steps as rs
    from shared.schemas.missions import Segment
    # The steps the compiler will send once ROUTE_GAITS is widened.
    monkeypatch.setattr(rs, "ROUTE_GAITS", rs.QUAD_LEG_GAITS)
    pr = _load("program_runner")
    mission = route([wp("wp-1", 0, 0), wp("wp-2", 1, 0), wp("wp-3", 2, 0), wp("wp-4", 3, 0)], [
        Segment(from_waypoint_id="wp-1", to_waypoint_id="wp-2", gait="flying_trot"),
        Segment(from_waypoint_id="wp-3", to_waypoint_id="wp-4", gait="rl"),
    ])
    steps = compile_route(mission, site_map(), "quad").steps
    clock = _Clock()
    gate = pr.RunGate(clock, sleep=clock.sleep)
    fx = _Effects(pr, gate)
    order = []
    send, navigate = fx.send, fx.navigate

    async def recording_send(step):
        order.append(step.params.get("state"))
        return await send(step)

    async def recording_navigate(step):
        order.append(step.waypointId)
        return await navigate(step)

    fx.send, fx.navigate = recording_send, recording_navigate
    outcome = asyncio.run(pr.run_program(steps, fx, gate))
    assert outcome.result == "completed", outcome
    assert order == ["wp-1", "flying_trot", "wp-2", "wp-3", "rl", "wp-4"]
