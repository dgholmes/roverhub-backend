"""Users, kept in one JSON file like the rest of this service's stores.

A handful of people per organisation, so one file read once and rewritten
atomically on change is plenty. Passwords are stored only as scrypt hashes.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .passwords import MIN_PASSWORD_LENGTH, hash_password, verify_password

OrgRole = Literal["owner", "admin", "member"]
SiteRole = Literal["viewer", "operator", "supervisor", "technician", "engineer"]

# A site_roles key that means every site, present and future. Used for the
# first owner, so a fresh install can reach the robots it already has.
ALL_SITES = "*"

DEFAULT_USERS_FILE = str(Path(__file__).resolve().parent.parent / "data" / "users.json")
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class User(BaseModel):
    user_id: str
    username: str
    display_name: str
    org_role: OrgRole = "member"
    site_roles: Dict[str, SiteRole] = Field(default_factory=dict)
    audit_access: bool = False
    disabled: bool = False
    password_hash: str
    # Bumped on password change, disable and "sign out everywhere"; a token
    # carrying an older version is refused (tokens.py).
    token_version: int = 1
    created_at: str = Field(default_factory=_now)
    last_login_at: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _username_shape(cls, v: str) -> str:
        v = v.strip().lower()
        if not _USERNAME.match(v):
            raise ValueError("username: 2-64 characters, lowercase letters, digits, '.', '_' or '-'")
        return v

    def site_role(self, site_id: Optional[str]) -> Optional[SiteRole]:
        if site_id is not None and site_id in self.site_roles:
            return self.site_roles[site_id]
        return self.site_roles.get(ALL_SITES)

    def public(self) -> dict:
        """What the API returns: everything but the hash."""
        return self.model_dump(exclude={"password_hash"})


class PasswordTooShort(ValueError):
    pass


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShort(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


class UserStore:
    def __init__(self, path: str = DEFAULT_USERS_FILE):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._users: Dict[str, User] = {}
        self._stamp: Optional[tuple] = None
        self._refresh()

    def _refresh(self) -> None:
        """Re-read the file if something else changed it -- the
        `python -m auth.manage` CLI writes it while the server runs, and the
        server must neither miss that user nor overwrite them on its next save."""
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            self._users, self._stamp = {}, None
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        raw = json.loads(self._path.read_text(encoding="utf-8") or "[]")
        self._users = {u["user_id"]: User.model_validate(u) for u in raw}
        self._stamp = stamp

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps([u.model_dump() for u in self._users.values()], indent=2)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".users-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        stat = self._path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)

    # --- reads -------------------------------------------------------------

    def list(self) -> List[User]:
        with self._lock:
            self._refresh()
            return sorted(self._users.values(), key=lambda u: u.username)

    def get(self, user_id: str) -> Optional[User]:
        with self._lock:
            self._refresh()
            return self._users.get(user_id)

    def by_username(self, username: str) -> Optional[User]:
        name = username.strip().lower()
        with self._lock:
            self._refresh()
            return next((u for u in self._users.values() if u.username == name), None)

    def is_empty(self) -> bool:
        with self._lock:
            self._refresh()
            return not self._users

    def active_owner_count(self) -> int:
        with self._lock:
            self._refresh()
            return sum(1 for u in self._users.values() if u.org_role == "owner" and not u.disabled)

    # --- writes ------------------------------------------------------------

    def create(self, *, username: str, display_name: str, password: str, org_role: OrgRole = "member",
               site_roles: Optional[Dict[str, SiteRole]] = None, audit_access: bool = False) -> User:
        check_password_rules(password)
        with self._lock:
            self._refresh()
            if self.by_username(username) is not None:
                raise ValueError(f"username {username.strip().lower()!r} is taken")
            user = User(user_id=f"user-{uuid.uuid4().hex[:12]}", username=username,
                        display_name=display_name.strip() or username, org_role=org_role,
                        site_roles=site_roles or {}, audit_access=audit_access,
                        password_hash=hash_password(password))
            self._users[user.user_id] = user
            self._save()
            return user

    def update(self, user_id: str, **changes) -> User:
        with self._lock:
            self._refresh()
            user = self._users[user_id]
            if changes.get("disabled") and not user.disabled:
                changes["token_version"] = user.token_version + 1
            updated = User.model_validate({**user.model_dump(), **changes})
            self._users[user_id] = updated
            self._save()
            return updated

    def set_password(self, user_id: str, password: str) -> User:
        check_password_rules(password)
        with self._lock:
            self._refresh()
            user = self._users[user_id]
            return self.update(user_id, password_hash=hash_password(password),
                               token_version=user.token_version + 1)

    def revoke_tokens(self, user_id: str) -> User:
        with self._lock:
            self._refresh()
            return self.update(user_id, token_version=self._users[user_id].token_version + 1)

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._refresh()
            self._users.pop(user_id, None)
            self._save()

    def record_login(self, user_id: str) -> None:
        with self._lock:
            self._refresh()
            if user_id in self._users:
                self._users[user_id] = self._users[user_id].model_copy(update={"last_login_at": _now()})
                self._save()

    def check_credentials(self, username: str, password: str) -> Optional[User]:
        from .passwords import DUMMY_HASH

        user = self.by_username(username)
        if user is None:
            verify_password(password, DUMMY_HASH)
            return None
        if not verify_password(password, user.password_hash) or user.disabled:
            return None
        return user
