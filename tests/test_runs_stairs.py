"""A program's Stairs steps (schema 0.0.21): refused on save and at the start
of a run, in one sentence the console shows verbatim, when they cannot run --
missions/stairs_checks.py, through missions/router.py and runs/dispatch.py."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.commands import CommandStore
from missions import stairs_checks
from missions.route_steps import RouteNotRunnable, compile_route
from missions.router import build_missions_router
from robots.registry import RobotRegistry
from runs.dispatch import RunDispatcher
from runs.router import build_runs_router
from runs.store import RunStore
from shared.schemas.maps import MapZone
from shared.schemas.missions import GroundConstraints, MissionLibraryEntry, ProgramSteps
from tasks.store import TaskStore
from tests.test_route_steps import _rect, route, site_map, wp

ROBOT = "rover-j"
NOW = "2026-11-12T10:00:00Z"

# A flight along +x between landings at (0, 0) and (3, 0), 0.5 m clear of it.
FLIGHT = MapZone(zone_id="s-main", map_id="map-office", kind="stairs", label="Main stairs",
                 polygon=_rect(0.5, -0.6, 2.5, 0.6),
                 stairs={"bottom_xy_m": (0.0, 0.0), "top_xy_m": (3.0, 0.0)})
SITE = MapZone(zone_id="z-site", map_id="map-office", kind="geofence", label="site", polygon=_rect(-9, -9, 9, 9))
NO_GO = MapZone(zone_id="z-bin", map_id="map-office", kind="no_go", label="bin", polygon=_rect(4, 4, 5, 5))


def stairs_step(zone="s-main", direction="up", block="st"):
    return {"kind": "stairs", "blockId": block, "label": "Up the main stairs", "zoneId": zone,
            "direction": direction}


def program(steps=None, stairs_allowed=True, compiled=True):
    steps = [stairs_step()] if steps is None else steps
    return MissionLibraryEntry(
        mission_id="m-prog", name="Upstairs", type="patrol", kind="program", creator="x", updated_at="t",
        map_id="map-office", site_id="site-a", program_state='{"blocks":{}}',
        program_steps=ProgramSteps.model_validate({"compiler_version": 1, "quad": steps, "wheel": []})
        if compiled else None,
        constraints=GroundConstraints(stairs_allowed=stairs_allowed))


class Env:
    def __init__(self, tmp_path, schema="0.0.21", robot_type="quad", nav_map_id=None, smap="default"):
        self.registry = RobotRegistry(offline_after_s=None)
        self.registry.upsert_from_registration(ROBOT, "site-a", robot_type, "b-1", schema, nav_map_id=nav_map_id)
        self.registry.update_state(ROBOT, "STAND", "balance_stand", bridge_boot_id="boot-a")
        if smap == "default":
            smap = site_map(zones=[FLIGHT, SITE, NO_GO])
        self.maps = {} if smap is None else {"map-office": smap}
        self.published = []
        self.dispatcher = RunDispatcher(
            self.registry, RunStore(str(tmp_path / "runs")), TaskStore(str(tmp_path / "tasks")), CommandStore(),
            lambda robot_id, command: self.published.append(command),
            clock_iso=lambda: NOW, new_run_id=lambda m, r: f"run-{m}-{r}", map_lookup=self.maps.get)
        app = FastAPI()
        app.include_router(build_runs_router(str(tmp_path / "runs"), dispatcher=self.dispatcher))
        self.client = TestClient(app)

    def start(self, mission=None, steps=None):
        mission = mission if mission is not None else program()
        steps = [stairs_step()] if steps is None else steps
        return self.client.post(f"/robots/{ROBOT}/runs", json={
            "mission": mission.model_dump(mode="json"), "steps": steps, "source": "editor",
            "started_by": "operator-dev"})


# --- starting a run -----------------------------------------------------------------


def test_a_program_that_climbs_on_a_ready_robot_is_sent_with_its_stairs_step(tmp_path):
    env = Env(tmp_path)
    response = env.start()
    assert response.status_code == 201, response.text
    (command,) = env.published
    assert command.params["steps"] == [{**stairs_step(), "speedRatio": 30}]


def test_a_nested_stairs_step_is_found_and_checked(tmp_path):
    env = Env(tmp_path)
    nested = [{"kind": "if", "blockId": "i", "label": "if", "condition": {"kind": "standing"},
               "then": [], "else": [{"kind": "repeat", "blockId": "r", "label": "r", "times": 2,
                                     "body": [stairs_step(zone="nope")]}]}]
    response = env.start(steps=nested)
    assert response.status_code == 409
    assert "the stairs nope, which the map Office 3F does not have" in response.json()["detail"]


@pytest.mark.parametrize("make_env, mission, steps, phrase", [
    (dict(), program(stairs_allowed=False), None, "Stairs allowed is off"),
    (dict(robot_type="wheel"), None, None, "four-legged robots"),
    (dict(schema="0.0.20"), None, None, "Stairs steps need 0.0.21"),
    (dict(schema=None), None, None, "it runs schema unknown"),
    (dict(smap=None), None, None, "is not in the map store"),
    (dict(smap=site_map(zones=[FLIGHT], status="draft")), None, None, "is a draft"),
    (dict(smap=site_map(zones=[FLIGHT], status="archived")), None, None, "is archived"),
    (dict(), None, [stairs_step(zone="z-bin")], "which is a no-go zone, not a staircase"),
    (dict(), None, [stairs_step(zone="z-missing")], "does not have"),
    (dict(nav_map_id="map-lobby"), None, None, "localised on the map map-lobby"),
])
def test_a_stairs_step_that_cannot_run_is_refused_in_words(tmp_path, make_env, mission, steps, phrase):
    env = Env(tmp_path, **make_env)
    response = env.start(mission=mission, steps=steps)
    assert response.status_code == 409, response.text
    assert phrase in response.json()["detail"]
    assert env.published == []


@pytest.mark.parametrize("zones, direction, phrase", [
    # The bottom landing 0.2 m from the flight: inside the bridge's 0.4 m margin.
    ([MapZone(zone_id="s-main", map_id="map-office", kind="stairs", label="Main stairs",
              polygon=_rect(0.2, -0.6, 2.5, 0.6), stairs={"bottom_xy_m": (0.0, 0.0), "top_xy_m": (3.0, 0.0)})],
     "up", "bottom landing of the stairs Main stairs is within 0.4 m of its own flight"),
    # The top landing under a no-go zone.
    ([FLIGHT, MapZone(zone_id="z-bin", map_id="map-office", kind="no_go", label="bin",
                      polygon=_rect(2.8, -0.3, 3.4, 0.3))],
     "down", "top landing of the stairs Main stairs is within 0.4 m of the zone bin"),
])
def test_a_landing_the_robot_cannot_walk_to_is_refused(tmp_path, zones, direction, phrase):
    env = Env(tmp_path, smap=site_map(zones=zones))
    response = env.start(steps=[stairs_step(direction=direction)])
    assert response.status_code == 409
    assert phrase in response.json()["detail"]


def test_the_other_landing_is_not_checked_for_this_direction(tmp_path):
    # Only the landing the step starts from has to be reachable.
    zones = [FLIGHT, MapZone(zone_id="z-bin", map_id="map-office", kind="no_go", label="bin",
                             polygon=_rect(2.8, -0.3, 3.4, 0.3))]
    env = Env(tmp_path, smap=site_map(zones=zones))
    assert env.start(steps=[stairs_step(direction="up")]).status_code == 201


def test_a_program_without_stairs_is_untouched_by_any_of_it(tmp_path):
    # An old bridge, no map: still fine for a program that does not climb.
    env = Env(tmp_path, schema="0.0.11", smap=None)
    plain = [{"kind": "wait", "blockId": "w", "label": "w", "seconds": 1}]
    assert env.start(mission=program(steps=plain, stairs_allowed=False), steps=plain).status_code == 201


# --- routes: a waypoint on the stairs -----------------------------------------------


def test_a_route_waypoint_on_a_flight_is_refused():
    smap = site_map(zones=[FLIGHT, SITE])
    with pytest.raises(RouteNotRunnable, match="on or within 0.4 m of the stairs Main stairs"):
        compile_route(route([wp("wp-1", 1.5, 0.0, "Mid-flight")]), smap, "quad")
    # Its landing, clear of the margin, is fine.
    assert compile_route(route([wp("wp-1", 0.0, 0.0, "Bottom")]), smap, "quad").steps


# --- saving a program ---------------------------------------------------------------


def _save(tmp_path, mission, maps=None):
    app = FastAPI()
    app.include_router(build_missions_router(str(tmp_path / "missions"),
                                             map_lookup=None if maps is None else maps.get))
    return TestClient(app).put(f"/missions/{mission.mission_id}", json=mission.model_dump(mode="json"))


def test_saving_a_program_that_climbs_needs_stairs_allowed(tmp_path):
    response = _save(tmp_path, program(stairs_allowed=False))
    assert response.status_code == 422
    assert "Stairs allowed is off" in response.json()["detail"]
    assert _save(tmp_path, program()).status_code == 200


def test_saving_checks_the_zone_when_the_map_is_known(tmp_path):
    maps = {"map-office": site_map(zones=[FLIGHT, NO_GO])}
    response = _save(tmp_path, program(steps=[stairs_step(zone="z-bin")]), maps)
    assert response.status_code == 422 and "not a staircase" in response.json()["detail"]
    assert _save(tmp_path, program(), maps).status_code == 200
    # A map the store does not have (yet) is the start's to refuse, not the save's.
    assert _save(tmp_path, program(steps=[stairs_step(zone="z-bin")]), {}).status_code == 200


def test_a_program_saved_before_it_was_compiled_has_no_stairs_to_check(tmp_path):
    assert _save(tmp_path, program(stairs_allowed=False, compiled=False)).status_code == 200


def test_stairs_steps_are_found_in_both_robot_types_compiles():
    mission = program()
    mission.program_steps.wheel = list(mission.program_steps.quad)
    assert len(stairs_checks.program_stairs(mission)) == 2
