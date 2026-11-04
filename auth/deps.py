"""Who is asking, and may they: the guard every router is mounted behind.

Three kinds of caller:
- a signed-in **user**, by session token (Authorization header, or the
  session cookie on GET so <img>/<video>/download links work);
- an edge **device**, by its device key (DEVICE_KEYS), for the few routes a
  bridge calls -- snapshots, run events, fetching a Speak clip;
- **dev**, when AUTH_ENABLED is off: everyone, allowed everything, exactly as
  before sign-in existed.
"""
from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Request

from .policy import ROUTES, CAPABILITIES, Rule, role_can
from .tokens import DEFAULT_TOKEN_TTL_S, TokenIssuer, load_secret
from .users import ALL_SITES, User, UserStore, DEFAULT_USERS_FILE

logger = logging.getLogger("roverhub.auth")

SESSION_COOKIE = "rh_session"
# Who commands are attributed to when sign-in is off -- the identity the
# console used before it existed, so nothing downstream changes.
DEV_USER_ID = "operator-dev"

# (site_id, robot_id) a request's path refers to, or None for either.
SiteResolver = Callable[[Dict[str, str]], Tuple[Optional[str], Optional[str]]]


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def parse_device_keys(raw: str) -> Dict[str, str]:
    """`rover-pi:key1,rover-jetson:key2` -> {robot_id: key}."""
    keys: Dict[str, str] = {}
    for entry in raw.split(","):
        robot_id, sep, key = entry.strip().partition(":")
        if sep and robot_id.strip() and key.strip():
            keys[robot_id.strip()] = key.strip()
    return keys


@dataclass(frozen=True)
class Principal:
    kind: str  # "user" | "device" | "dev"
    id: str
    user: Optional[User] = None

    def can(self, capability: str, site_id: Optional[str]) -> bool:
        if self.kind == "dev":
            return True
        if self.user is None:
            return False
        if site_id is not None:
            return role_can(self.user.site_role(site_id), capability)
        # Site unknown from the path: any site where they hold it.
        return any(role_can(r, capability) for r in self.user.site_roles.values())

    @property
    def is_org_admin(self) -> bool:
        return self.kind == "dev" or (self.user is not None and self.user.org_role in ("owner", "admin"))

    def visible_site(self, site_id: Optional[str]) -> bool:
        """Whether this caller may see anything at `site_id`."""
        if self.kind in ("dev", "device"):
            return True
        if self.user is None or site_id is None:
            return False
        return self.user.site_role(site_id) is not None


class Auth:
    def __init__(self, *, enabled: bool, users: UserStore, issuer: TokenIssuer,
                 device_keys: Optional[Dict[str, str]] = None, broker_service_password: str = "",
                 resolve_site: Optional[SiteResolver] = None, secure_cookie: bool = False,
                 allow_unkeyed_bridges: bool = False):
        self.enabled = enabled
        self.users = users
        self.issuer = issuer
        self.device_keys = device_keys or {}
        self.broker_service_password = broker_service_password
        self.resolve_site: SiteResolver = resolve_site or (lambda params: (None, None))
        self.secure_cookie = secure_cookie
        # Development only: people sign in, but bridges that have no device key
        # yet may still call the device routes, as they could before sign-in.
        self.allow_unkeyed_bridges = allow_unkeyed_bridges

    @classmethod
    def from_env(cls, resolve_site: Optional[SiteResolver] = None) -> "Auth":
        users_file = os.environ.get("USERS_FILE", DEFAULT_USERS_FILE)
        secret_file = str(Path(users_file).parent / "auth_secret")
        enabled = _truthy(os.environ.get("AUTH_ENABLED"))
        auth = cls(
            enabled=enabled,
            users=UserStore(users_file),
            issuer=TokenIssuer(load_secret(secret_file) if enabled else "sign-in-disabled",
                               ttl_s=int(os.environ.get("AUTH_TOKEN_TTL_S", DEFAULT_TOKEN_TTL_S))),
            device_keys=parse_device_keys(os.environ.get("DEVICE_KEYS", "")),
            broker_service_password=os.environ.get("BROKER_SERVICE_PASSWORD", ""),
            resolve_site=resolve_site,
            secure_cookie=_truthy(os.environ.get("AUTH_COOKIE_SECURE")),
            allow_unkeyed_bridges=_truthy(os.environ.get("AUTH_ALLOW_UNKEYED_BRIDGES")),
        )
        if enabled and auth.users.is_empty():
            logger.warning("sign-in is on and there are no users yet. Create the first owner with: "
                           "python -m auth.manage create-owner <username>")
        if enabled and auth.allow_unkeyed_bridges:
            logger.warning("AUTH_ALLOW_UNKEYED_BRIDGES is on: anyone on the network can upload snapshots, "
                           "post run events and fetch Speak clips without a key. Development only -- "
                           "give each bridge CLOUD_API_KEY and turn this off before production.")
        elif enabled and not auth.device_keys:
            logger.warning("sign-in is on and DEVICE_KEYS is empty: edge bridges cannot upload "
                           "snapshots, report run events or fetch Speak clips")
        return auth

    # --- identifying the caller -------------------------------------------

    def device_for_key(self, key: str) -> Optional[str]:
        for robot_id, expected in self.device_keys.items():
            if hmac.compare_digest(key.encode(), expected.encode()):
                return robot_id
        return None

    def user_for_token(self, token: str) -> Optional[User]:
        claims = self.issuer.verify(token)
        if claims is None:
            return None
        user = self.users.get(claims.user_id)
        if user is None or user.disabled or user.token_version != claims.version:
            return None
        return user

    def identify(self, request: Request) -> Optional[Principal]:
        if not self.enabled:
            return Principal(kind="dev", id=DEV_USER_ID)
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token and request.method in ("GET", "HEAD"):
            # Cookies only authorise reads, so a page on another port of this
            # host cannot make the browser send a command for it.
            token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            return None
        device = self.device_for_key(token) if "." not in token else None
        if device is not None:
            return Principal(kind="device", id=device)
        user = self.user_for_token(token)
        return Principal(kind="user", id=user.user_id, user=user) if user else None

    # --- the guard ----------------------------------------------------------

    async def guard(self, request: Request) -> None:
        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)
        rule = ROUTES.get((request.method, template))
        if rule is None:
            # A route with no rule is refused rather than left open, so a new
            # endpoint cannot ship unprotected. Off, everything stays open.
            if self.enabled:
                logger.error("no access rule for %s %s; refusing", request.method, template)
                raise HTTPException(status_code=403, detail="this endpoint has no access rule")
            rule = Rule(public=True)
        principal = self.identify(request)
        request.state.principal = principal
        site_id, robot_id = self.resolve_site(dict(request.path_params))
        request.state.site_id = site_id
        if rule.public or (principal is not None and principal.kind == "dev"):
            return
        if principal is None and rule.device and self.allow_unkeyed_bridges:
            return
        if principal is None:
            raise HTTPException(status_code=401, detail="sign in to continue",
                                headers={"WWW-Authenticate": "Bearer"})
        if principal.kind == "device":
            if not rule.device:
                raise HTTPException(status_code=403, detail="edge devices may not call this")
            if robot_id is not None and robot_id != principal.id:
                raise HTTPException(status_code=403, detail=f"this device key belongs to {principal.id}")
            return
        if rule.signed_in:
            return
        if rule.org_admin:
            if not principal.is_org_admin:
                raise HTTPException(status_code=403, detail="only an organisation owner or admin can do this")
            return
        if rule.capabilities and any(principal.can(c, site_id) for c in rule.capabilities):
            return
        raise HTTPException(status_code=403, detail=_forbidden(rule.capabilities, site_id))


def _forbidden(capabilities, site_id: Optional[str]) -> str:
    holders = sorted(set().union(*(CAPABILITIES[c] for c in capabilities))) if capabilities else []
    where = f" at {site_id}" if site_id else ""
    return f"your role{where} does not allow this (needs: {', '.join(holders) or 'device key'})"


def principal_of(request: Request) -> Optional[Principal]:
    return getattr(request.state, "principal", None)


def acting_user_id(request: Request, claimed: Optional[str]) -> Optional[str]:
    """Who an action is attributed to. With sign-in on it is always the
    signed-in user, whatever the body claims; off, the body's value stands."""
    principal = principal_of(request)
    if principal is None or principal.kind == "dev":
        return claimed
    return principal.id


__all__ = ["Auth", "Principal", "principal_of", "acting_user_id", "SESSION_COOKIE", "DEV_USER_ID",
           "ALL_SITES", "parse_device_keys"]
