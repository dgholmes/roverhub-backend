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
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Iterable, Optional, Tuple

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


class BridgeBindings:
    """Which robot's device key writes each bridge's registration topic.

    A registration travels on roverhub/admin/<bridge_id>/register and its body
    names the robot, so on its own the body -- not the credential -- would say
    which robot it is. The broker asks /mqtt/acl before it accepts a publish,
    with the publisher's username, which for a device is its robot id: that is
    where the credential is visible. The first device key to write a bridge id
    binds it; no other device may write it afterwards, and a registration on a
    bound bridge id is believed only for the robot bound to it.

    In memory, like the robot registry. After a backend restart nothing is
    bound until each bridge's next registration is checked again (the broker
    caches ACL answers for its own TTL), so an unbound registration is accepted
    -- unless a bound bridge already speaks for that robot (Auth.registration_allowed).
    """

    MAX = 4096

    def __init__(self):
        self._owners: "OrderedDict[str, str]" = OrderedDict()
        self._lock = threading.Lock()

    def bind(self, bridge_id: str, robot_id: str) -> bool:
        """Record that `robot_id`'s key writes `bridge_id`. False if another
        robot's key already holds it."""
        with self._lock:
            owner = self._owners.get(bridge_id)
            if owner is not None and owner != robot_id:
                return False
            self._owners[bridge_id] = robot_id
            self._owners.move_to_end(bridge_id)
            while len(self._owners) > self.MAX:
                self._owners.popitem(last=False)
            return True

    def owner(self, bridge_id: Optional[str]) -> Optional[str]:
        if bridge_id is None:
            return None
        with self._lock:
            return self._owners.get(bridge_id)


@dataclass(frozen=True)
class AuditScope:
    """What part of the audit log a caller may read. `all_sites`: every site's
    rows; otherwise those of `sites`. `org_rows`: rows with no site (sign-ins,
    user changes)."""
    all_sites: bool
    sites: FrozenSet[str]
    org_rows: bool
    # With all_sites: sites named with a role that does not qualify, which wins
    # over the '*' one there.
    excluded: FrozenSet[str] = frozenset()

    def allows_site(self, site_id: Optional[str]) -> bool:
        if site_id is None:
            return self.org_rows
        if self.all_sites:
            return site_id not in self.excluded
        return site_id in self.sites


# Who reads a site's audit rows: the /audit rule's capabilities (policy.py).
_AUDIT_CAPABILITIES = ("override", "configure")


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
        self.bridges = BridgeBindings()

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

    # --- device identity on the broker ---------------------------------------

    @property
    def device_identity_enforced(self) -> bool:
        """Devices sign in to the broker with their own keys, so its ACL calls
        say who writes what. Off (or with no keys) the broker is anonymous and
        there is nothing to check a registration against."""
        return self.enabled and bool(self.device_keys)

    def registration_allowed(self, bridge_id: str, robot_id: str, known_bridge_id: Optional[str]) -> bool:
        """May a registration on `bridge_id`'s topic speak for `robot_id`?
        `known_bridge_id` is the bridge the registry already has for it."""
        if not self.device_identity_enforced:
            return True
        owner = self.bridges.owner(bridge_id)
        if owner is not None:
            return owner == robot_id
        # Unbound: the backend restarted and the broker answered from its cache.
        # Accept, unless a bridge the robot's own key writes already speaks for
        # it -- then this one is a stranger claiming it.
        if (known_bridge_id is not None and known_bridge_id != bridge_id
                and self.bridges.owner(known_bridge_id) == robot_id):
            return False
        return True

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
        if rule.auditors and (principal.is_org_admin or (principal.user is not None and principal.user.audit_access)):
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


def site_visible(request: Request, site_id: Optional[str]) -> bool:
    """Whether the caller may see a record at `site_id` in a list. Everyone
    when sign-in is off (and in a router mounted without the guard, as in unit
    tests); a signed-in person only at sites where they hold a role. A record
    with no site is shown to anyone who holds a role somewhere."""
    principal = principal_of(request)
    if principal is None or principal.kind in ("dev", "device"):
        return True
    if site_id is None:
        return principal.user is not None and bool(principal.user.site_roles)
    return principal.visible_site(site_id)


def visible_sites(request: Request) -> Optional[FrozenSet[str]]:
    """The sites the caller may see, for a list filtered in the database:
    None means every site (sign-in off, or a '*' role)."""
    principal = principal_of(request)
    if principal is None or principal.kind in ("dev", "device"):
        return None
    if principal.user is None:
        return frozenset()
    if ALL_SITES in principal.user.site_roles:
        return None
    return frozenset(principal.user.site_roles)


def visible(request: Request, items: Iterable, site_of: Callable[[object], Optional[str]]) -> list:
    """The `items` the caller may see, judged by each one's site."""
    return [item for item in items if site_visible(request, site_of(item))]


def require_site_capability(request: Request, site_id: Optional[str], capabilities: Tuple[str, ...]) -> None:
    """For a write whose body names its site. The guard checks the site in the
    path (or the stored record's), so a body naming another site -- a create,
    or an edit that moves a record -- is checked here: 403 unless the caller
    holds one of `capabilities` at the body's site."""
    principal = principal_of(request)
    if principal is None or principal.kind in ("dev", "device"):
        return
    if not any(principal.can(c, site_id) for c in capabilities):
        raise HTTPException(status_code=403, detail=_forbidden(capabilities, site_id))


def audit_scope(principal: Optional[Principal]) -> AuditScope:
    """Which audit rows a caller may read. Sign-in off: all of them. Otherwise
    a site's rows where they are a supervisor or engineer (the /audit rule's
    capabilities), or hold any role and have audit access; and rows with no
    site -- sign-ins, user and role changes -- only for org owners/admins and
    holders of audit access."""
    if principal is None or principal.kind == "dev":
        return AuditScope(all_sites=True, sites=frozenset(), org_rows=True)
    user = principal.user
    if user is None:
        return AuditScope(all_sites=False, sites=frozenset(), org_rows=False)

    def qualifies(role) -> bool:
        return role is not None and (user.audit_access or any(role_can(role, c) for c in _AUDIT_CAPABILITIES))

    named = {s: r for s, r in user.site_roles.items() if s != ALL_SITES}
    org_rows = principal.is_org_admin or user.audit_access
    if qualifies(user.site_roles.get(ALL_SITES)):
        return AuditScope(all_sites=True, sites=frozenset(), org_rows=org_rows,
                          excluded=frozenset(s for s, r in named.items() if not qualifies(r)))
    return AuditScope(all_sites=False, sites=frozenset(s for s, r in named.items() if qualifies(r)),
                      org_rows=org_rows)


def acting_user_id(request: Request, claimed: Optional[str]) -> Optional[str]:
    """Who an action is attributed to. With sign-in on it is always the
    signed-in user, whatever the body claims; off, the body's value stands."""
    principal = principal_of(request)
    if principal is None or principal.kind == "dev":
        return claimed
    return principal.id


__all__ = ["Auth", "AuditScope", "BridgeBindings", "Principal", "principal_of", "acting_user_id",
           "audit_scope", "require_site_capability", "site_visible", "visible", "visible_sites",
           "SESSION_COOKIE",
           "DEV_USER_ID", "ALL_SITES", "parse_device_keys"]
