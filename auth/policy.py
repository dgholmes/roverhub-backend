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
    # again, so it is the same capability as starting one. Resuming a run an
    # E-Stop paused (0.0.33) is RESUME_ESTOP_CAPABILITIES instead, decided per
    # request in control/router.py from the robot's latest state.
    "RESUME": ("dispatchPatrols",),
    # Surveying from the console (schema 0.0.23, shared/schemas/survey.py).
    # A survey ends in a draft map the robot uploads with its own device key
    # (POST /map-surveys, then PUT planning), and one that names a map_id
    # replaces that map's bundle: both are what POST /map-surveys means by site
    # configuration, so starting one is `configure` -- the engineer's, as for an
    # upload by hand. Supervisors (`editZones`) review and freeze the draft; they
    # do not make it, so one person does not both make a map and sign it off.
    # Stopping is `configure` too, not _STOPPING: STOP_SURVEY moves and stops
    # nothing (E-Stop and Release control do that, and E-Stop does not end the
    # recording), and both of its outcomes -- a draft map, or the recording
    # thrown away -- are the survey's, not a safety decision.
    "START_SURVEY": ("configure",),
    "STOP_SURVEY": ("configure",),
    # Re-running the moving-object search replaces a floor's "probably moved"
    # layer, which is the map editor's PUT .../moved: `editZones` or `configure`.
    "FIND_MOVING": ("editZones", "configure"),
    # Where the lidar sits changes where the robot believes it is, and the
    # calibration mode lets the bridge change it by itself: robot configuration,
    # the engineer's (shared/schemas/sensors.py).
    "SET_LIDAR_MOUNT": ("configure",),
    "SET_CALIBRATION_MODE": ("configure",),
    # "Send to robot" (schema 0.0.26): replacing the map files a robot plans,
    # localises and enforces its zones on is robot configuration, as copying
    # them by hand was -- the engineer's. Drawing the zones stays `editZones`;
    # this only carries what was drawn (and signed off) to the robot.
    "SYNC_MAP": ("configure",),
    # A stuck walk (schema 0.0.27). NAV_RESOLVE answers it -- walk a route
    # the robot offers, retry, or cancel -- and is not listed: like NAV_GOTO,
    # whose walk it continues, it takes the `drive` default. What the robot
    # does when nobody answers is a robot setting, stored on the robot and
    # followed by every walk and mission there: the engineer's, like the
    # lidar's mount.
    "SET_NAV_STUCK_POLICY": ("configure",),
    # The map ready checklist (schema 0.0.29). Processing a survey again ends
    # in a draft map, as START_SURVEY does; keeping or deleting a recording and
    # skipping the moving-object search are the survey's; loading a map onto a
    # robot on another map (or none) is SYNC_MAP's sibling; moving a robot to
    # another site re-points everything it publishes. All robot configuration,
    # the engineer's. SET_SITE needs `configure` at the site it moves to as
    # well (control/router.py `_check_set_site`).
    "REPROCESS_SURVEY": ("configure",),
    "SET_SURVEY_KEEP": ("configure",),
    "DELETE_SURVEY_RECORDING": ("configure",),
    "SKIP_MOVING_SEARCH": ("configure",),
    "USE_NAV_MAP": ("configure",),
    "SET_SITE": ("configure",),
    # Switching AI detection on or off, and which detectors run (schema
    # 0.0.31). Listed although it is the `drive` default, so the choice is
    # visible: it is the operator's switch, as SET_OBSTACLE_AVOIDANCE (which
    # takes the default) is -- both are flipped mid-run from LiveOps
    # (MISSION_SHARED_COMMAND_TYPES) and move nothing. Not `configure`: the
    # setting is kept on the device, but it is what the operator watching
    # wants now, not how the robot is set up.
    "SET_DETECTION": ("drive",),
}


def command_capabilities(command_type: str) -> Tuple[str, ...]:
    return COMMAND_CAPABILITIES.get(command_type, ("drive",))


# RESUME for a run an E-Stop paused (0.0.33, the operator's ruling): anyone
# who may drive, because the person at the console after an E-Stop need not
# be the one who pressed it, and the pause was a safety stop rather than a
# patrol decision. Only when the robot's latest /state says that very run is
# paused for "estop"; with no state, or any other pause, RESUME stays
# COMMAND_CAPABILITIES' (refuse rather than widen). Mirrored in the console's
# role check (roverhub-frontend org/roles.ts / commandPolicy).
RESUME_ESTOP_CAPABILITIES: Tuple[str, ...] = ("drive",)


# --- Routes -----------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """What a route needs.

    `capabilities`: a signed-in user needs any one of these at the request's
    site (or, when the site cannot be told from the path, at any site).
    `device`: an edge device's key is accepted. `device_own_site`: an edge
    device's key is accepted for records at the site its robot registered at,
    and nowhere else: the guard checks a site the path names, the handler one
    the body names (deps.py `require_site_capability`, `site_visible`). Unlike
    `device`, it is never opened to unkeyed bridges (AUTH_ALLOW_UNKEYED_BRIDGES):
    without a key there is no robot, so no site to hold it to. `org_admin`: only an org owner
    or admin. `public`: nothing at all. `signed_in`: any signed-in user.
    `auditors`: also anyone holding the "audit access" flag (roles.ts's folded
    Auditor role) and org owners/admins -- the handler then narrows what they
    see (auth/deps.py `audit_scope`).
    """
    capabilities: Tuple[str, ...] = ()
    device: bool = False
    device_own_site: bool = False
    org_admin: bool = False
    public: bool = False
    signed_in: bool = False
    auditors: bool = False


_READ = Rule(capabilities=("watchLive",))
# Read like _READ, and by a robot's own device key at its own site.
_READ_OR_OWN_DEVICE = Rule(capabilities=("watchLive",), device_own_site=True)

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
    # The engineer-tools switch (2026-11-18): any signed-in person reaches the
    # handler, which refuses everyone but org owners and admins with its own
    # message (auth/router.py). While on, Principal.can adds the `engineer`
    # role's capabilities at the sites where the caller holds a role.
    ("POST", "/auth/engineer-tools"): Rule(signed_in=True),
    # The broker's own checks. They take credentials in the body and answer
    # yes or no, so they are public in the same way /auth/login is.
    ("POST", "/mqtt/auth"): Rule(public=True),
    ("POST", "/mqtt/superuser"): Rule(public=True),
    ("POST", "/mqtt/acl"): Rule(public=True),

    ("GET", "/users"): Rule(org_admin=True),
    # Display names for ids, for histories (2026-11-18): names only, so anyone
    # signed in may ask. Edge devices may not (Rule.signed_in is people only).
    ("GET", "/users/names"): Rule(signed_in=True),
    ("POST", "/users"): Rule(org_admin=True),
    ("PATCH", "/users/{user_id}"): Rule(org_admin=True),
    ("DELETE", "/users/{user_id}"): Rule(org_admin=True),
    ("POST", "/users/{user_id}/password"): Rule(org_admin=True),
    ("POST", "/users/{user_id}/sign-out"): Rule(org_admin=True),

    # Sites (sites/, 2026-11-26). Anyone signed in may list and read sites;
    # the handler shows each person only the sites they may see (every site
    # for org owners and admins, else where they hold a role -- sites/router.py
    # `may_see_site`) and answers 404 for any other. Making, editing and
    # deleting a site is organisation management, like users: org owners and
    # admins only (the operator's call, recorded in the design spec).
    ("GET", "/sites"): Rule(signed_in=True),
    ("GET", "/sites/{site_id}"): Rule(signed_in=True),
    ("POST", "/sites"): Rule(org_admin=True),
    ("PATCH", "/sites/{site_id}"): Rule(org_admin=True),
    ("DELETE", "/sites/{site_id}"): Rule(org_admin=True),

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
    # Asking a robot for its own log (schema 0.0.22) is viewing, not acting on
    # it: the same as reading its telemetry, at the robot's site. The answer
    # arrives on the broker's .../logs topic, which the ACL opens to the same
    # people (auth/mqtt_hooks.py). It moves nothing, so a viewer may ask.
    ("POST", "/robots/{robot_id}/logs/request"): _READ,

    # Audio for Speak: made by an operator, fetched by the bridge.
    ("POST", "/robots/{robot_id}/audio-clips"): Rule(capabilities=("drive",)),
    ("GET", "/tts/capability"): _READ,
    ("POST", "/tts"): Rule(capabilities=("drive",)),
    # Checked at the site of the robot the clip was uploaded for (its id
    # names it: api/app.py _resolve_site), like any per-robot read; any
    # device key may fetch it, as a mission's Speak step keeps its clip when
    # the mission runs on another robot.
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
    # A robot reads back the draft it uploaded (its floor's size, origin and
    # survey.json) to build the planning grid on it -- at its own site only.
    ("GET", "/maps/{map_id}"): _READ_OR_OWN_DEVICE,
    ("GET", "/maps/{map_id}/floors/{index}/{kind}"): _READ_OR_OWN_DEVICE,
    ("POST", "/maps"): Rule(capabilities=("configure",)),
    ("PUT", "/maps/{map_id}/zones"): Rule(capabilities=("editZones",)),
    # A floor's home (0.0.33): where Return home sends the robot, so the same
    # judgement as where it may walk -- the supervisor's, like zones.
    ("PUT", "/maps/{map_id}/floors/{index}/home"): Rule(capabilities=("editZones",)),
    ("DELETE", "/maps/{map_id}/floors/{index}/home"): Rule(capabilities=("editZones",)),
    # A floor's planning grid (the map Nav2 plans on) is read like its survey
    # grid. Removing one is like drawing zones: both say where the robot may
    # walk. The map editor's saves -- the brushed planning grid, the "probably
    # moved" layer and cloud edits (maps/cloud_edit.py) -- each make a new
    # version and drop the map to draft, so a supervisor still signs it off
    # (PUT status, `editZones`) before missions use it: the supervisor and the
    # engineer who uploaded the survey may both clean it.
    # The robot's own survey pipeline (roverhub-edge survey_pipeline/, schema
    # 0.0.23) uploads its draft with its device key: the survey, then the
    # planning grid, and -- after FIND_MOVING -- a new moved layer. Only for
    # maps at the site its robot registered at (`device_own_site`). A robot
    # never removes a planning grid, freezes a map or edits its cloud.
    ("GET", "/maps/{map_id}/floors/{index}/planning"): _READ_OR_OWN_DEVICE,
    ("PUT", "/maps/{map_id}/floors/{index}/planning"): Rule(capabilities=("editZones", "configure"),
                                                            device_own_site=True),
    ("DELETE", "/maps/{map_id}/floors/{index}/planning"): Rule(capabilities=("editZones",)),
    ("PUT", "/maps/{map_id}/floors/{index}/moved"): Rule(capabilities=("editZones", "configure"),
                                                         device_own_site=True),
    ("POST", "/maps/{map_id}/floors/{index}/cloud-edits"): Rule(capabilities=("editZones", "configure")),
    ("PUT", "/maps/{map_id}/status"): Rule(capabilities=("editZones",)),
    # The map ready checklist (0.0.29). Trimming a floor to the walked area is
    # a map-editor save (a new version, back to draft) that decides where the
    # floor ends, and naming the map and its floors is curating it: both the
    # supervisor's, like zones. Who confirmed "no zones needed" when freezing
    # is PUT status above (maps/router.py `_zones_gate`).
    ("POST", "/maps/{map_id}/floors/{index}/trim"): Rule(capabilities=("editZones",)),
    ("PATCH", "/maps/{map_id}"): Rule(capabilities=("editZones",)),
    ("DELETE", "/maps/{map_id}"): Rule(capabilities=("configure",)),
    # The survey pipeline (maps/survey.py, maps/jobs.py). A survey upload and a
    # version restore replace the map's bundle, as an upload does, so they are
    # site configuration. The quality report is read like the map; running it
    # changes nothing a robot uses, so the engineer who uploads and the
    # supervisor who reviews may both start one. Freezing a map whose report
    # failed is PUT status above plus `override` at the map's site, checked in
    # the handler (maps/router.py _quality_gate) because only the body says so.
    # A job's site is in its record, not its path: the handlers hide jobs at
    # sites the caller cannot see.
    ("POST", "/map-surveys"): Rule(capabilities=("configure",), device_own_site=True),
    ("GET", "/map-jobs"): _READ_OR_OWN_DEVICE,
    ("GET", "/map-jobs/{job_id}"): _READ_OR_OWN_DEVICE,
    ("GET", "/maps/{map_id}/quality"): _READ,
    ("POST", "/maps/{map_id}/quality"): Rule(capabilities=("editZones", "configure")),
    ("GET", "/maps/{map_id}/versions"): _READ,
    ("GET", "/maps/{map_id}/versions/{version}/floors/{index}/{kind}"): _READ,
    ("POST", "/maps/{map_id}/versions/{version}/restore"): Rule(capabilities=("configure",)),

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

    # Survey replay (surveys/, schema 0.0.28). The robot's survey pipeline
    # writes a survey's record, its takes and their files with its own device
    # key, at the site its robot registered at and for its own robot only (the
    # path's survey names the robot; a new one's body is checked by the
    # handler). People read them as they read runs. The same writes by a person
    # are edge tooling, the engineer's. Deleting a re-run take is too, and a
    # device never deletes one; the original take cannot be deleted at all.
    ("GET", "/surveys"): _READ,
    ("GET", "/surveys/{survey_id}"): _READ,
    ("PUT", "/surveys/{survey_id}"): Rule(capabilities=("edgeTools",), device_own_site=True),
    ("PUT", "/surveys/{survey_id}/takes/{take_id}"): Rule(capabilities=("edgeTools",), device_own_site=True),
    ("DELETE", "/surveys/{survey_id}/takes/{take_id}"): Rule(capabilities=("edgeTools",)),
    ("PUT", "/surveys/{survey_id}/takes/{take_id}/files/{name}"): Rule(capabilities=("edgeTools",),
                                                                       device_own_site=True),
    ("GET", "/surveys/{survey_id}/takes/{take_id}/files/{name}"): _READ,

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
    # A robot the registry does not know is checked at the site of its newest
    # stored sample (api/app.py _robot_site), and samples from sites the
    # reader cannot see are left out (history/router.py).
    ("GET", "/robots/{robot_id}/history"): _READ,
    ("GET", "/audit"): Rule(capabilities=("override", "configure"), auditors=True),
    ("GET", "/history/status"): Rule(capabilities=("seeHealth",)),
}

# HEAD follows GET wherever GET is allowed (browsers probe media with it).
for (_method, _path), _rule in list(ROUTES.items()):
    if _method == "GET":
        ROUTES.setdefault(("HEAD", _path), _rule)
