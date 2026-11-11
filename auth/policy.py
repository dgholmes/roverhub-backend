"""Who may do what: the console's role model, enforced here.

CAPABILITIES mirrors roverhub-frontend/src/org/roles.ts (adopted 2026-10-15,
OD-7). The browser uses that table to show and hide controls; this one decides.
tests/test_auth_policy.py compares the two when the frontend is checked out
beside this repo, so they cannot drift silently.

Site roles are per site. Org roles (owner, admin, member) only govern the
organisation itself -- who can manage users -- and grant no robot access on
their own, as on the Team page.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

ALL_ROLES = ("viewer", "operator", "supervisor", "technician", "engineer")

CAPABILITIES: Dict[str, FrozenSet[str]] = {
    # LiveOps
    "watchLive": frozenset(ALL_ROLES),
    "estop": frozenset({"operator", "supervisor", "technician", "engineer"}),
    "drive": frozenset({"operator", "supervisor", "engineer"}),
    "getUp": frozenset({"operator", "supervisor", "technician", "engineer"}),
    "override": frozenset({"supervisor"}),
    # Missions
    "planPatrols": frozenset({"supervisor"}),
    "dispatchPatrols": frozenset({"supervisor"}),
    # Fleet
    "seeHealth": frozenset(ALL_ROLES),
    "runTests": frozenset({"technician", "engineer"}),
    "edgeTools": frozenset({"engineer"}),
    # Site
    "editZones": frozenset({"supervisor"}),
    "configure": frozenset({"engineer"}),
}


def role_can(role: Optional[str], capability: str) -> bool:
    return role is not None and role in CAPABILITIES[capability]


# --- Commands ---------------------------------------------------------------
#
# Each command type names the capabilities that allow it (any one is enough).
# Stopping is never harder than moving: anyone who may E-Stop may also stop a
# mission or a clip. Anything not listed needs `drive`.
_STOPPING = ("estop",)
COMMAND_CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    "ESTOP": _STOPPING,
    "STOP_MISSION": _STOPPING,
    "STOP_SPEAK": _STOPPING,
    # Cancelling a walk-to-point is stopping, so it is never harder than starting
    # one. NAV_GOTO itself is driving the robot and takes the `drive` default.
    "NAV_CANCEL": _STOPPING,
    # Placing the robot on the map is not a stop: it changes where the robot
    # believes it is, which redirects every goal that follows. Same as driving.
    "RESET_POSITION": ("drive",),
    # Releasing an E-Stop leaves the robot lying down (`ready`); standing it
    # up again is the "get a robot back up" capability technicians hold.
    "RESET_ESTOP": ("getUp",),
    "SET_STATE": ("drive", "getUp"),
    # A photo moves nothing, but it is still an action on the robot, so a
    # watch-only viewer may not take one.
    "SNAPSHOT": ("estop",),
    "START_MISSION": ("dispatchPatrols",),
    # Retry a run the robot paused as blocked (0.0.16): it sets a patrol going
    # again, so it is the same capability as starting one.
    "RESUME": ("dispatchPatrols",),
}


def command_capabilities(command_type: str) -> Tuple[str, ...]:
    return COMMAND_CAPABILITIES.get(command_type, ("drive",))


# --- Routes -----------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """What a route needs.

    `capabilities`: a signed-in user needs any one of these at the request's
    site (or, when the site cannot be told from the path, at any site).
    `device`: an edge device's key is accepted. `org_admin`: only an org owner
    or admin. `public`: nothing at all. `signed_in`: any signed-in user.
    `auditors`: also anyone holding the "audit access" flag (roles.ts's folded
    Auditor role) and org owners/admins -- the handler then narrows what they
    see (auth/deps.py `audit_scope`).
    """
    capabilities: Tuple[str, ...] = ()
    device: bool = False
    org_admin: bool = False
    public: bool = False
    signed_in: bool = False
    auditors: bool = False


_READ = Rule(capabilities=("watchLive",))

# Keyed by (method, FastAPI path template). A route missing here is refused
# (see deps.py), so a new endpoint cannot ship unprotected by accident.
ROUTES: Dict[Tuple[str, str], Rule] = {
    ("GET", "/healthz"): Rule(public=True),
    ("POST", "/auth/login"): Rule(public=True),
    ("GET", "/auth/config"): Rule(public=True),
    ("POST", "/auth/logout"): Rule(public=True),
    ("GET", "/auth/me"): Rule(signed_in=True),
    ("POST", "/auth/refresh"): Rule(signed_in=True),
    ("POST", "/auth/password"): Rule(signed_in=True),
    # The broker's own checks. They take credentials in the body and answer
    # yes or no, so they are public in the same way /auth/login is.
    ("POST", "/mqtt/auth"): Rule(public=True),
    ("POST", "/mqtt/superuser"): Rule(public=True),
    ("POST", "/mqtt/acl"): Rule(public=True),

    ("GET", "/users"): Rule(org_admin=True),
    ("POST", "/users"): Rule(org_admin=True),
    ("PATCH", "/users/{user_id}"): Rule(org_admin=True),
    ("DELETE", "/users/{user_id}"): Rule(org_admin=True),
    ("POST", "/users/{user_id}/password"): Rule(org_admin=True),
    ("POST", "/users/{user_id}/sign-out"): Rule(org_admin=True),

    # Robots
    ("GET", "/robots"): _READ,
    ("GET", "/robots/pending"): _READ,
    ("POST", "/robots/{robot_id}/accept"): Rule(capabilities=("configure",)),
    ("POST", "/robots/{robot_id}/reject"): Rule(capabilities=("configure",)),
    ("DELETE", "/robots/{robot_id}"): Rule(capabilities=("configure",)),
    # The per-type check happens in the handler (control/router.py); this is
    # the floor: someone who can act on a robot at all.
    ("POST", "/robots/{robot_id}/commands"): Rule(capabilities=("estop", "drive", "getUp")),
    ("GET", "/robots/{robot_id}/commands/{command_id}"): _READ,

    # Audio for Speak: made by an operator, fetched by the bridge.
    ("POST", "/robots/{robot_id}/audio-clips"): Rule(capabilities=("drive",)),
    ("GET", "/tts/capability"): _READ,
    ("POST", "/tts"): Rule(capabilities=("drive",)),
    ("GET", "/audio-clips/{clip_id}"): Rule(capabilities=("watchLive",), device=True),

    # Snapshots: taken by the bridge, browsed by people.
    ("POST", "/robots/{robot_id}/snapshots"): Rule(device=True),
    ("GET", "/snapshots"): _READ,
    ("GET", "/snapshots/{snapshot_id}"): _READ,

    # Missions and tasks
    ("GET", "/missions"): _READ,
    ("GET", "/missions/{mission_id}"): _READ,
    ("PUT", "/missions/{mission_id}"): Rule(capabilities=("planPatrols",)),
    ("DELETE", "/missions/{mission_id}"): Rule(capabilities=("planPatrols",)),
    ("GET", "/tasks"): _READ,
    ("GET", "/tasks/occurrences"): _READ,
    ("GET", "/tasks/{task_id}"): _READ,
    ("PUT", "/tasks/{task_id}"): Rule(capabilities=("dispatchPatrols",)),
    ("DELETE", "/tasks/{task_id}"): Rule(capabilities=("dispatchPatrols",)),
    ("POST", "/tasks/{task_id}/run-now"): Rule(capabilities=("dispatchPatrols",)),

    # Site maps: everyone who can see a site may see its floor plans; uploading
    # a survey or deleting a map is site configuration. Drawing zones and freezing
    # a map are one judgement -- a person has looked at it, and "active" is what
    # missions plan against -- so both need `editZones`, which supervisors hold
    # and engineers do not: the engineer who uploads a survey (`configure`) does
    # not also sign it off, and a re-upload drops an active map back to draft.
    ("GET", "/maps"): _READ,
    ("GET", "/maps/{map_id}"): _READ,
    ("GET", "/maps/{map_id}/floors/{index}/{kind}"): _READ,
    ("POST", "/maps"): Rule(capabilities=("configure",)),
    ("PUT", "/maps/{map_id}/zones"): Rule(capabilities=("editZones",)),
    # A floor's planning grid (the map Nav2 plans on) is read like its survey
    # grid, and set or removed like zones are drawn: both say where the robot
    # may walk, and neither changes the map's status.
    ("GET", "/maps/{map_id}/floors/{index}/planning"): _READ,
    ("PUT", "/maps/{map_id}/floors/{index}/planning"): Rule(capabilities=("editZones",)),
    ("DELETE", "/maps/{map_id}/floors/{index}/planning"): Rule(capabilities=("editZones",)),
    ("PUT", "/maps/{map_id}/status"): Rule(capabilities=("editZones",)),
    ("DELETE", "/maps/{map_id}"): Rule(capabilities=("configure",)),

    # Runs
    ("POST", "/robots/{robot_id}/runs"): Rule(capabilities=("dispatchPatrols",)),
    ("PUT", "/runs/{run_id}"): Rule(capabilities=("dispatchPatrols",)),
    ("POST", "/runs/{run_id}/events"): Rule(capabilities=("dispatchPatrols",), device=True),
    ("GET", "/runs"): _READ,
    ("GET", "/runs/{run_id}"): _READ,
    ("GET", "/runs/{run_id}/events"): _READ,
    ("PUT", "/runs/{run_id}/files/{rel:path}"): Rule(capabilities=("dispatchPatrols",), device=True),
    ("GET", "/runs/{run_id}/files/{rel:path}"): _READ,
    ("GET", "/runs/{run_id}/zip"): _READ,

    # History (history/router.py). Everyone who can see a site may read its
    # incidents and a robot's history. Answering an incident -- acknowledge,
    # resolve -- is for the people who act on robots there: the same floor as
    # sending a command (estop, drive, getUp), so a watch-only viewer cannot.
    # The audit log is for those who run the site: supervisors (`override`)
    # and engineers (`configure`), plus anyone given audit access and the org's
    # owners/admins. Each sees only the rows they may (auth/deps.py
    # `audit_scope`): their sites' rows, and org-level rows (sign-ins, user
    # changes) only for auditors and org admins. The store's status is fleet health.
    ("GET", "/incidents"): _READ,
    ("GET", "/incidents/{incident_id}"): _READ,
    ("POST", "/incidents/{incident_id}/acknowledge"): Rule(capabilities=("estop", "drive", "getUp")),
    ("POST", "/incidents/{incident_id}/resolve"): Rule(capabilities=("estop", "drive", "getUp")),
    ("GET", "/robots/{robot_id}/history"): _READ,
    ("GET", "/audit"): Rule(capabilities=("override", "configure"), auditors=True),
    ("GET", "/history/status"): Rule(capabilities=("seeHealth",)),
}

# HEAD follows GET wherever GET is allowed (browsers probe media with it).
for (_method, _path), _rule in list(ROUTES.items()):
    if _method == "GET":
        ROUTES.setdefault(("HEAD", _path), _rule)
