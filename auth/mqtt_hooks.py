"""The broker asks this service who may connect and what they may touch.

Mosquitto runs the mosquitto-go-auth plugin in HTTP mode (infra/), which posts
here on every connect and (cached) on every subscribe, publish and delivery.
Answer by status code: 200 allows, 403 denies.

Who connects:
- **this backend**: username `roverhub-backend`, BROKER_SERVICE_PASSWORD. Superuser.
- **an edge bridge**: username = its robot_id, password = its device key. May
  publish and read only its own robot's topics, plus its registration.
- **a browser**: username = the signed-in user's id, password = their session
  token. Read-only, and only the sites where they hold a role. Browsers never
  publish: commands go through REST, where roles are checked per command.
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Response
from pydantic import BaseModel

from .deps import Auth

logger = logging.getLogger("roverhub.auth.mqtt")

SERVICE_USERNAME = "roverhub-backend"

# mosquitto-go-auth's `acc` values (mosquitto's MOSQ_ACL_*).
ACC_READ, ACC_WRITE, ACC_READWRITE, ACC_SUBSCRIBE = 1, 2, 3, 4

# Per-robot topic suffixes, from docs/02-system-architecture.md.
_ROBOT_TOPICS = {"telemetry", "state", "heartbeat", "lower_state", "commands", "ack", "run"}
# What a bridge receives rather than sends.
_BRIDGE_READS = {"commands"}


class AuthRequest(BaseModel):
    username: str = ""
    password: str = ""
    clientid: str = ""


class SuperuserRequest(BaseModel):
    username: str = ""


class AclRequest(BaseModel):
    username: str = ""
    clientid: str = ""
    topic: str = ""
    acc: int = 0


def _ok() -> Response:
    return Response(status_code=200)


def _no() -> Response:
    return Response(status_code=403)


def _segment_matches(filter_segment: str, value: str) -> bool:
    return filter_segment in ("+", "#") or filter_segment == value


def _bridge_allowed(robot_id: str, topic: str, acc: int) -> bool:
    parts = topic.split("/")
    if parts[:2] == ["roverhub", "admin"] and len(parts) == 4 and parts[3] == "register":
        # The registration topic is keyed by bridge id, which is not the robot
        # id; the payload names the robot. A known limit: a device key can
        # write any registration. It cannot touch another robot's commands.
        return acc in (ACC_WRITE, ACC_READ, ACC_READWRITE, ACC_SUBSCRIBE)
    if len(parts) != 4 or parts[0] != "roverhub" or parts[2] != robot_id or parts[3] not in _ROBOT_TOPICS:
        return False
    if parts[3] in _BRIDGE_READS:
        return acc in (ACC_READ, ACC_SUBSCRIBE)
    return acc in (ACC_WRITE, ACC_READ, ACC_SUBSCRIBE, ACC_READWRITE)


def _browser_allowed(auth: Auth, user_id: str, topic: str, acc: int) -> bool:
    if acc not in (ACC_READ, ACC_SUBSCRIBE):
        return False
    user = auth.users.get(user_id)
    if user is None or user.disabled or not user.site_roles:
        return False
    parts = topic.split("/")
    if parts[:2] == ["roverhub", "admin"]:
        # Registrations say which robots exist and at which site. Seeing the
        # list is the same as GET /robots, which any signed-in user may call.
        return len(parts) == 4 and _segment_matches(parts[2], "x") and parts[3] == "register"
    if len(parts) != 4 or parts[0] != "roverhub" or parts[3] not in _ROBOT_TOPICS - {"commands"}:
        return False
    site = parts[1]
    if site in ("+", "#"):
        # A subscription spanning sites. Allowed; each delivered message is
        # checked again with its real site (ACC_READ), so only theirs arrive.
        return acc == ACC_SUBSCRIBE
    return user.site_role(site) is not None


def build_mqtt_hooks_router(auth: Auth) -> APIRouter:
    router = APIRouter()

    def _is_service(username: str, password: Optional[str] = None) -> bool:
        if not auth.broker_service_password or username != SERVICE_USERNAME:
            return False
        return password is None or hmac.compare_digest(password.encode(), auth.broker_service_password.encode())

    @router.post("/mqtt/auth")
    def mqtt_auth(body: AuthRequest):
        if _is_service(body.username, body.password):
            return _ok()
        expected = auth.device_keys.get(body.username)
        if expected and hmac.compare_digest(body.password.encode(), expected.encode()):
            return _ok()
        user = auth.user_for_token(body.password)
        if user is not None and user.user_id == body.username:
            return _ok()
        logger.info("broker refused %r (client %r)", body.username, body.clientid)
        return _no()

    @router.post("/mqtt/superuser")
    def mqtt_superuser(body: SuperuserRequest):
        # Called only after /mqtt/auth accepted the same connection, so the
        # username alone is enough here.
        return _ok() if _is_service(body.username) else _no()

    @router.post("/mqtt/acl")
    def mqtt_acl(body: AclRequest):
        if body.username in auth.device_keys:
            allowed = _bridge_allowed(body.username, body.topic, body.acc)
        else:
            allowed = _browser_allowed(auth, body.username, body.topic, body.acc)
        if not allowed:
            logger.info("broker denied %r acc=%s on %s", body.username, body.acc, body.topic)
        return _ok() if allowed else _no()

    return router
