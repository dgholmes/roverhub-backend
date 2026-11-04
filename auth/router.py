"""Sign-in, the user's own account, and user management."""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .deps import DEV_USER_ID, SESSION_COOKIE, Auth, principal_of
from .users import ALL_SITES, OrgRole, PasswordTooShort, SiteRole


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class PasswordReset(BaseModel):
    new_password: str = Field(max_length=256)


class NewUser(BaseModel):
    username: str
    display_name: str = ""
    password: str = Field(max_length=256)
    org_role: OrgRole = "member"
    site_roles: Dict[str, SiteRole] = Field(default_factory=dict)
    audit_access: bool = False


class UserChanges(BaseModel):
    display_name: Optional[str] = None
    org_role: Optional[OrgRole] = None
    site_roles: Optional[Dict[str, SiteRole]] = None
    audit_access: Optional[bool] = None
    disabled: Optional[bool] = None


class LoginThrottle:
    """Five wrong passwords for one username inside five minutes locks that
    username for a minute. Per username rather than per address: the office
    shares one NAT address, and one person's typos must not lock out the rest."""

    def __init__(self, limit: int = 5, window_s: float = 300, lock_s: float = 60, clock=time.monotonic):
        self._limit, self._window_s, self._lock_s, self._clock = limit, window_s, lock_s, clock
        self._failures: Dict[str, List[float]] = {}
        self._locked_until: Dict[str, float] = {}
        self._mutex = threading.Lock()

    def retry_after(self, username: str) -> float:
        with self._mutex:
            return max(0.0, self._locked_until.get(username, 0.0) - self._clock())

    def failed(self, username: str) -> None:
        now = self._clock()
        with self._mutex:
            recent = [t for t in self._failures.get(username, []) if now - t < self._window_s] + [now]
            self._failures[username] = recent
            if len(recent) >= self._limit:
                self._locked_until[username] = now + self._lock_s
                self._failures[username] = []

    def succeeded(self, username: str) -> None:
        with self._mutex:
            self._failures.pop(username, None)
            self._locked_until.pop(username, None)


def _dev_user() -> dict:
    return {"user_id": DEV_USER_ID, "username": DEV_USER_ID, "display_name": "Developer",
            "org_role": "owner", "site_roles": {ALL_SITES: "supervisor"}, "audit_access": True,
            "disabled": False}


def build_auth_router(auth: Auth, throttle: Optional[LoginThrottle] = None) -> APIRouter:
    router = APIRouter()
    throttle = throttle or LoginThrottle()

    def _set_cookie(response: Response, token: str, max_age: int) -> None:
        response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True,
                            samesite="lax", secure=auth.secure_cookie, path="/")

    @router.get("/auth/config")
    def config():
        """Lets the console decide whether to show the sign-in page at all."""
        return {"auth_enabled": auth.enabled, "needs_first_owner": auth.enabled and auth.users.is_empty()}

    @router.post("/auth/login")
    def login(body: LoginRequest, response: Response):
        if not auth.enabled:
            raise HTTPException(status_code=404, detail="sign-in is off on this server")
        name = body.username.strip().lower()
        wait = throttle.retry_after(name)
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"too many attempts; try again in {int(wait) + 1} s",
                                headers={"Retry-After": str(int(wait) + 1)})
        user = auth.users.check_credentials(name, body.password)
        if user is None:
            throttle.failed(name)
            raise HTTPException(status_code=401, detail="wrong username or password")
        throttle.succeeded(name)
        auth.users.record_login(user.user_id)
        token, expires_at = auth.issuer.issue(user.user_id, user.token_version)
        _set_cookie(response, token, max(0, expires_at - int(time.time())))
        return {"access_token": token, "token_type": "bearer", "expires_at": expires_at,
                "user": auth.users.get(user.user_id).public()}

    @router.post("/auth/logout", status_code=204)
    def logout():
        # Tokens are not tracked server-side; this clears the cookie and the
        # console forgets its copy. "Sign out everywhere" is the admin's
        # POST /users/{id}/sign-out, or changing the password.
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @router.get("/auth/me")
    def me(request: Request):
        principal = principal_of(request)
        if principal is None or principal.kind == "dev":
            return {"auth_enabled": False, "user": _dev_user()}
        if principal.kind == "device":
            raise HTTPException(status_code=403, detail="device keys have no user")
        return {"auth_enabled": True, "user": principal.user.public()}

    @router.post("/auth/password", status_code=204)
    def change_own_password(body: PasswordChange, request: Request):
        principal = principal_of(request)
        if principal is None or principal.user is None:
            raise HTTPException(status_code=400, detail="sign-in is off; there is no password to change")
        if auth.users.check_credentials(principal.user.username, body.current_password) is None:
            raise HTTPException(status_code=403, detail="current password is wrong")
        try:
            auth.users.set_password(principal.user.user_id, body.new_password)
        except PasswordTooShort as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(status_code=204)

    # --- user management (org owner / admin) -------------------------------

    def _acting(request: Request):
        return principal_of(request)

    def _guard_owner_change(request: Request, target_role: Optional[str], current_role: Optional[str]) -> None:
        """Only an owner may make or unmake an owner."""
        principal = _acting(request)
        if principal is None or principal.kind == "dev":
            return
        if "owner" in (target_role, current_role) and principal.user.org_role != "owner":
            raise HTTPException(status_code=403, detail="only an owner can add or change an owner")

    def _require_user(user_id: str):
        user = auth.users.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        return user

    @router.get("/users")
    def list_users():
        return [u.public() for u in auth.users.list()]

    @router.post("/users", status_code=201)
    def create_user(body: NewUser, request: Request):
        _guard_owner_change(request, body.org_role, None)
        try:
            user = auth.users.create(username=body.username, display_name=body.display_name,
                                     password=body.password, org_role=body.org_role,
                                     site_roles=body.site_roles, audit_access=body.audit_access)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return user.public()

    @router.patch("/users/{user_id}")
    def update_user(user_id: str, body: UserChanges, request: Request):
        user = _require_user(user_id)
        changes = body.model_dump(exclude_none=True)
        _guard_owner_change(request, changes.get("org_role"), user.org_role)
        losing_owner = user.org_role == "owner" and not user.disabled and (
            changes.get("org_role", "owner") != "owner" or changes.get("disabled"))
        if losing_owner and auth.users.active_owner_count() <= 1:
            raise HTTPException(status_code=409, detail="this is the last owner; make someone else owner first")
        return auth.users.update(user_id, **changes).public()

    @router.delete("/users/{user_id}", status_code=204)
    def delete_user(user_id: str, request: Request):
        user = _require_user(user_id)
        _guard_owner_change(request, None, user.org_role)
        if user.org_role == "owner" and not user.disabled and auth.users.active_owner_count() <= 1:
            raise HTTPException(status_code=409, detail="this is the last owner; make someone else owner first")
        auth.users.delete(user_id)
        return Response(status_code=204)

    @router.post("/users/{user_id}/password", status_code=204)
    def reset_password(user_id: str, body: PasswordReset, request: Request):
        user = _require_user(user_id)
        _guard_owner_change(request, None, user.org_role)
        try:
            auth.users.set_password(user_id, body.new_password)
        except PasswordTooShort as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(status_code=204)

    @router.post("/users/{user_id}/sign-out", status_code=204)
    def sign_out_everywhere(user_id: str, request: Request):
        user = _require_user(user_id)
        _guard_owner_change(request, None, user.org_role)
        auth.users.revoke_tokens(user_id)
        return Response(status_code=204)

    return router
