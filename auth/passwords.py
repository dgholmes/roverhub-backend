"""Password hashing with the standard library's scrypt. No extra dependency,
and the parameters travel with the hash so they can be raised later."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

_N, _R, _P = 2 ** 14, 8, 1
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when a username does not exist, so a missing user costs the
# same time as a wrong password and response timing does not reveal who exists.
DUMMY_HASH = hash_password("not-a-real-password")
