"""missions/route_steps.py: a route compiled into the steps the bridge runs.

The console shows every refusal sentence verbatim, so each is pinned exactly.
"""
import math

import pytest
from pydantic import TypeAdapter

from missions.route_steps import (
    CompiledRoute, RouteNotRunnable, compile_route, heading_to_yaw,
)
from shared.schemas.maps import MapFloor, MapZone, SiteMap
from shared.schemas.missions import (
    MissionLibraryEntry, NavStep, ProgramStep, Segment, Waypoint, WaypointAction,
)

STEPS = TypeAdapter(list[ProgramStep])


def _floor(index=0, z=0.0):
    # 20 m x 20 m at 5 cm, lower-left corner at (-10, -10).
    return MapFloor(index=index, floor_z_m=z, resolution_m=0.05, origin_xy_m=(-10.0, -10.0),
                    size_cells=(400, 400), free_cells=1, occupied_cells=1, unknown_cells=1)


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def site_map(floors=None, zones=None, status="active"):
    return SiteMap(
        map_id="map-office", site_id="site-a", name="Office 3F", source="slam",
        extent_min=(-10, -10), extent_max=(10, 10), created_at="t", updated_at="t",
        floors=[_floor()] if floors is None else floors, status=status,
        zones=[
            MapZone(zone_id="z-stairs", map_id="map-office", kind="no_go", label="stairs",
                    polygon=_rect(4, 4, 6, 6)),
            MapZone(zone_id="z-site", map_id="map-office", kind="geofence", label="site",
                    polygon=_rect(-9, -9, 9, 9)),
        ] if zones is None else zones,
    )


def wp(wid, x, y, label=None, actions=(), **extra):
    return Waypoint(waypoint_id=wid, label=label or wid.upper(), x_m=x, y_m=y,
                    actions=[WaypointAction(**a) for a in actions], **extra)


def route(waypoints=None, segments=(), **extra):
    if waypoints is None:
        waypoints = [
            wp("wp-1", 0, 0, "Gate A"),
            wp("wp-2", 3, 0, "Lobby", heading_deg=90, actions=[
                {"type": "snapshot", "label": "Photo", "params": {"camera": "rear"}},
                {"type": "wait", "label": "Hold", "params": {"seconds": 5}},
                {"type": "voice_prompt", "label": "Hello",
                 "params": {"clip_id": "clip-1.wav", "volume": 70}},
            ]),
            wp("wp-3", 3, 2, "Desk", tolerance_m=0.1),
        ]
    return MissionLibraryEntry(
        mission_id="m-route", name="Lobby round", type="patrol", kind="route", creator="x",
        updated_at="t", map_id="map-office", site_id="site-a", waypoints=waypoints,
        segments=list(segments), **extra)


def compiled(mission=None, smap=None, robot_type="quad") -> CompiledRoute:
    return compile_route(mission or route(), smap or site_map(), robot_type)


# -- the mapping -----------------------------------------------------------------

def test_each_waypoint_is_a_leg_then_its_actions_in_order():
    steps = compiled().steps
    assert [(s.kind, s.blockId) for s in steps] == [
        ("navigate", "wp:wp-1:go"),
        ("navigate", "wp:wp-2:go"),
        ("command", "wp:wp-2:a0"),
        ("wait", "wp:wp-2:a1"),
        ("command", "wp:wp-2:a2"),
        ("navigate", "wp:wp-3:go"),
    ]
    # And they are steps the bridge's own schema accepts, by alias, as sent.
    wire = [s.model_dump(by_alias=True) for s in steps]
    assert STEPS.validate_python(wire) == steps


def test_a_leg_carries_the_point_the_map_and_the_floor():
    leg = compiled().steps[1]
    assert isinstance(leg, NavStep)
    assert (leg.label, leg.waypointId) == ("Lobby", "wp-2")
    assert (leg.goal.x_m, leg.goal.y_m, leg.goal.map_id, leg.goal.floor_id) == (3, 0, "map-office", "0")
    assert leg.goal.yaw_rad == pytest.approx(0.0)     # heading 90 = east = map +x
    assert compiled().steps[0].goal.yaw_rad is None    # no heading: face the way it walked


def test_tolerance_never_goes_below_the_bridge_minimum():
    steps = compiled().steps
    assert steps[0].toleranceM == 0.5
    assert steps[-1].toleranceM == 0.3                 # authored 0.1


def test_actions_become_commands_and_waits():
    _, _, photo, hold, hello, _ = compiled().steps
    assert (photo.type, photo.params, photo.label) == ("SNAPSHOT", {"camera": "back"}, "Photo")
    assert hold.seconds == 5
    assert (hello.type, hello.params) == ("SPEAK", {"clip_id": "clip-1.wav", "volume": 70})


def test_the_front_camera_stays_front():
    mission = route([wp("wp-1", 0, 0, actions=[{"type": "snapshot", "label": "P", "params": {}}])])
    assert compiled(mission).steps[1].params == {"camera": "front"}


def test_leg_time_caps_and_the_run_cap():
    mission = route([wp("a", -8, 0), wp("b", -7, 0), wp("c", 1, 0), wp("e", 1, -8.5, actions=[
        {"type": "wait", "label": "w", "params": {"seconds": 20}}, {"type": "snapshot", "label": "p"}])])
    out = compiled(mission, site_map(zones=[]))
    legs = [s for s in out.steps if s.kind == "navigate"]
    # First leg: where the robot starts is unknown. Then 60 s + 8 s/m, 60..900.
    assert [leg.maxSeconds for leg in legs] == [300.0, 68.0, 124.0, 128.0]
    assert out.cap_s == pytest.approx((300 + 68 + 124 + 128 + 20 + 5) * 1.5)


def test_a_long_leg_is_capped_at_900_s():
    big = _floor().model_copy(update={"size_cells": (4000, 400)})     # 200 m along x
    mission = route([wp("a", 0, 0), wp("b", 150, 0)])
    assert compiled(mission, site_map(floors=[big], zones=[])).steps[1].maxSeconds == 900.0


def test_segment_speed_caps_the_leg_it_leads_into():
    mission = route(segments=[
        Segment(from_waypoint_id="wp-1", to_waypoint_id="wp-2", speed_mps=0.4),
        Segment(from_waypoint_id="wp-2", to_waypoint_id="wp-3", speed_mps=1.5, gait="walk"),
    ])
    legs = [s for s in compiled(mission).steps if s.kind == "navigate"]
    assert [leg.maxSpeedMps for leg in legs] == [None, 0.4, 1.0]   # above 1 m/s is held at 1


def test_the_floor_is_the_one_nearest_the_waypoints_height():
    smap = site_map(floors=[_floor(0, 0.0), _floor(1, 3.0)])
    mission = route([wp("a", 0, 0, z_m=2.6), wp("b", 1, 0)])
    legs = compiled(mission, smap).steps
    assert [leg.goal.floor_id for leg in legs] == ["1", "0"]


# -- heading ---------------------------------------------------------------------

@pytest.mark.parametrize("heading, yaw", [
    (0, math.pi / 2),        # up the screen = map +y
    (90, 0.0),               # right = map +x
    (180, -math.pi / 2),
    (270, math.pi),
    (45, math.pi / 4),
    (359, math.radians(91)),
])
def test_heading_is_a_compass_bearing_and_yaw_is_anticlockwise_from_x(heading, yaw):
    assert heading_to_yaw(heading) == pytest.approx(yaw)


def test_no_heading_is_no_yaw():
    assert heading_to_yaw(None) is None


# -- refusals, word for word -------------------------------------------------------

def _refusal(mission=None, smap=None, robot_type="quad") -> str:
    with pytest.raises(RouteNotRunnable) as caught:
        compiled(mission, smap, robot_type)
    return caught.value.message


def _one(**extra):
    return route([wp("wp-1", 0, 0, "Gate A", **extra)])


def _with_action(action_type, params=None):
    return _one(actions=[{"type": action_type, "label": "x", "params": params or {}}])


def test_a_wheel_robot_is_refused():
    assert _refusal(robot_type="wheel") == (
        "routes run only on four-legged robots for now, and this robot is a wheel robot; "
        "pick a four-legged robot to run it")


def test_a_route_with_no_waypoints_is_refused():
    assert _refusal(route([])) == "this route has no waypoints; add at least one in the mission editor"


def test_a_map_with_no_floor_is_refused():
    assert _refusal(smap=site_map(floors=[])) == (
        "the map Office 3F has no surveyed floor; upload its survey again in Missions › Maps")


def test_a_waypoint_off_the_surveyed_grid_is_refused():
    mission = route([wp("wp-1", 15, 0, "Car park")])
    assert _refusal(mission, site_map(zones=[])) == (
        "waypoint Car park is outside the surveyed area of the map Office 3F; "
        "move it onto the map in the mission editor")


def test_a_waypoint_in_a_no_go_zone_is_refused():
    mission = route([wp("wp-1", 5, 5, "Stairwell")])
    assert _refusal(mission) == (
        "waypoint Stairwell is in or within 0.4 m of the no-go zone stairs; "
        "move it further out in the mission editor")


def test_a_waypoint_just_outside_a_no_go_zone_but_within_the_margin_is_refused():
    mission = route([wp("wp-1", 3.7, 5, "Landing")])
    assert "no-go zone stairs" in _refusal(mission)


def test_a_waypoint_outside_the_site_boundary_is_refused():
    mission = route([wp("wp-1", -9.5, 0, "Fence")])
    assert _refusal(mission) == (
        "waypoint Fence is outside the site boundary site, or within 0.4 m of its edge; "
        "move it inside in the mission editor")


@pytest.mark.parametrize("action_type, name", [
    ("video_clip", "Video"),
    ("led_signal", "LED Signal"),
    ("operator_approval", "Operator Approval"),
    ("return_to_dock", "Return to Dock"),
])
def test_an_action_the_robot_cannot_run_yet_is_refused(action_type, name):
    assert _refusal(_with_action(action_type)) == (
        f"the {name} action at waypoint Gate A cannot run on the robot yet; "
        "remove it from the route to run this mission")


def test_an_announcement_with_no_clip_is_refused():
    assert _refusal(_with_action("voice_prompt", {"text": "hi"})) == (
        "the announcement at waypoint Gate A has nothing to say yet; "
        "record, type or pick a clip in the mission editor")


def test_a_leg_with_another_gait_is_refused():
    mission = route(segments=[Segment(from_waypoint_id="wp-1", to_waypoint_id="wp-2", gait="flying_trot")])
    assert _refusal(mission) == (
        "the leg into waypoint Lobby uses the flying_trot gait, and routes can only walk for now; "
        "set its gait back to walk in the mission editor")


def test_a_leg_with_a_posture_is_refused():
    mission = route(segments=[Segment(from_waypoint_id="wp-1", to_waypoint_id="wp-2", posture="crouch")])
    assert _refusal(mission) == (
        "the leg into waypoint Lobby sets a posture, which routes cannot do yet; "
        "clear it in the mission editor")


def test_a_leg_with_a_wheel_leg_mode_is_refused():
    mission = route(segments=[Segment(from_waypoint_id="wp-1", to_waypoint_id="wp-2", wheel_leg_mode="x")])
    assert _refusal(mission) == (
        "the leg into waypoint Lobby sets a wheel-leg mode, which routes cannot do yet; "
        "clear it in the mission editor")


def test_stairs_allowed_is_refused():
    mission = route(constraints={"stairs_allowed": True})
    assert _refusal(mission) == (
        "this route allows stairs, which the robot cannot climb on a route yet; "
        "turn off Stairs allowed in the mission's settings")


@pytest.mark.parametrize("policy, words", [
    ("slow", "slow down"), ("pause", "pause"), ("alert", "alert an operator"),
    ("require_takeover", "hand over to an operator"),
])
def test_a_people_policy_is_refused(policy, words):
    mission = route(constraints={"human_proximity_policy": policy})
    assert _refusal(mission) == (
        f"this route asks the robot to {words} when people are near, which it cannot do yet; "
        "set When people are near to Carry on in the mission's settings")


def test_timed_announcements_are_refused():
    mission = route(announcements=[{"announcement_id": "a", "clip_id": "c", "at": "09:00"}])
    assert _refusal(mission) == (
        "this route has timed announcements, which cannot play on the robot yet; "
        "remove them in the mission's Announcements tab")


def test_a_waypoint_with_no_label_is_named_by_its_id():
    mission = route([Waypoint(waypoint_id="wp-9", label="", x_m=5, y_m=5)])
    assert _refusal(mission).startswith("waypoint wp-9 is in or within")


def test_a_map_with_no_zones_accepts_any_point_on_its_grid():
    mission = route([wp("wp-1", 9.5, 9.5)])
    assert compiled(mission, site_map(zones=[])).steps[0].goal.x_m == 9.5
