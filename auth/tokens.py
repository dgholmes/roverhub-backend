"""Signed session tokens (HS256 JWT).

A token names the user and the user's `token_version`. Changing a password,
disabling the user or signing out everywhere bumps that version, which ends
every token issued before it -- so tokens can be revoked without a deny list.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import jwt

# An office shift. Long enough that nobody is signed out mid-patrol, short
# enough that a token copied off a shared machine dies by the next day.
DEFAULT_TOKEN_TTL_S = 12 * 60 * 60
_ALGORITHM = "HS256"
_ISSUER = "roverhub"


@dataclass(frozen=True)
class TokenClaims:
    user_id: str
    version: int
    expires_at: int


def load_secret(path: str) -> str:
    """AUTH_SECRET if set; otherwise a random secret kept in `path`, created on
    first use, so tokens survive a backend restart without anyone having to
    invent a secret."""
    env = os.environ.get("AUTH_SECRET", "").strip()
    if env:
        return env
    file = Path(path)
    if file.exists():
        return file.read_text(encoding="utf-8").strip()
    file.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    file.write_text(secret, encoding="utf-8")
    try:
        os.chmod(file, 0o600)
    except OSError:
        pass  # Windows: best effort
    return secret


class TokenIssuer:
    def __init__(self, secret: str, ttl_s: int = DEFAULT_TOKEN_TTL_S, clock=time.time):
        self._secret = secret
        self._ttl_s = ttl_s
        self._clock = clock

    def issue(self, user_id: str, version: int) -> tuple[str, int]:
        now = int(self._clock())
        expires_at = now + self._ttl_s
        token = jwt.encode(
            {"sub": user_id, "ver": version, "iat": now, "exp": expires_at, "iss": _ISSUER},
            self._secret, algorithm=_ALGORITHM,
        )
        return token, expires_at

    def verify(self, token: str) -> Optional[TokenClaims]:
        try:
            claims = jwt.decode(
                token, self._secret, algorithms=[_ALGORITHM], issuer=_ISSUER,
                # Expiry is checked below against self._clock, so tests can move time.
                options={"require": ["sub", "ver", "exp", "iss"], "verify_exp": False},
            )
        except jwt.PyJWTError:
            return None
        if claims["exp"] < self._clock():
            return None
        return TokenClaims(user_id=str(claims["sub"]), version=int(claims["ver"]), expires_at=int(claims["exp"]))
