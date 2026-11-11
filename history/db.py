"""The two engines history runs on, and the small dialect shim between them.

SQLite (standard library) is the default: a file at HISTORY_DB, no setup and
no new dependency. Postgres is used when DATABASE_URL starts with `postgres`,
through psycopg 3, imported only then (requirements-postgres.txt).

The SQL in store.py and migrations/ is written once, portably, with `?`
placeholders. What differs is translated here, and only here:

    placeholders     ?                        -> %s
    JSON columns     TEXT                     -> JSONB      ({{JSON}} in DDL, json_param() in SQL)
    autoincrement    INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL PRIMARY KEY  ({{ID}})
    floating point   REAL                     -> DOUBLE PRECISION  ({{REAL}})
    integer floor    CAST(x AS INTEGER)       -> FLOOR(x)  (floor_expr(); x >= 0 in every use)

Upserts use `INSERT ... ON CONFLICT (...) DO UPDATE SET col = excluded.col`,
which both engines accept as written.

Timestamps are UTC epoch seconds (floats) in every table; the API turns them
into ISO strings (iso()).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

# SQLite's own default: a writer waits this long for another's lock.
SQLITE_BUSY_TIMEOUT_MS = 5000
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class HistoryUnavailable(Exception):
    """History cannot run as configured; `message` says why, in a sentence."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    text = moment.isoformat(timespec="milliseconds" if moment.microsecond else "seconds")
    return text.replace("+00:00", "Z")


def epoch(value: Optional[str]) -> Optional[float]:
    """An ISO timestamp as epoch seconds; one with no offset is UTC. None if it
    is not a timestamp."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def redact(url: str) -> str:
    """A database URL fit for a log line: the password replaced."""
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url)


class Database:
    """Where history lives. `url` is a SQLite file path, or a postgres:// URL.
    `schema` (Postgres only) puts every table in that schema, created if absent:
    the tests give each test a schema of its own."""

    def __init__(self, url: str, schema: Optional[str] = None):
        self.url = url
        self.dialect = "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"
        if schema is not None and (self.dialect != "postgres" or not _SCHEMA_RE.match(schema)):
            raise ValueError(f"invalid schema {schema!r}")
        self.schema = schema
        self._psycopg = None

    @classmethod
    def from_env(cls, env=os.environ, default_path: str = "") -> "Database":
        url = (env.get("DATABASE_URL") or "").strip()
        if url.startswith("postgres"):
            return cls(url)
        return cls((env.get("HISTORY_DB") or "").strip() or default_path)

    def describe(self) -> str:
        return redact(self.url) if self.dialect == "postgres" else self.url

    # -- connections -------------------------------------------------------------

    def connect(self):
        """A new DB-API connection in autocommit mode: transactions are begun
        explicitly (store.transaction()), so DDL is transactional on both."""
        if self.dialect == "sqlite":
            directory = os.path.dirname(os.path.abspath(self.url))
            if not os.path.isdir(directory):
                raise HistoryUnavailable(f"the folder for HISTORY_DB does not exist: {directory}")
            conn = sqlite3.connect(self.url, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
                                   isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            return conn
        psycopg = self._driver()
        conn = psycopg.connect(self.url, autocommit=True, connect_timeout=5)
        if self.schema is not None:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            conn.execute(f'SET search_path TO "{self.schema}"')
        return conn

    def _driver(self):
        if self._psycopg is None:
            try:
                import psycopg  # noqa: PLC0415 -- only when Postgres is configured
            except ImportError as exc:
                raise HistoryUnavailable(
                    "DATABASE_URL names Postgres but psycopg is not installed; "
                    "pip install -r requirements-postgres.txt") from exc
            self._psycopg = psycopg
        return self._psycopg

    def is_disconnect(self, exc: BaseException) -> bool:
        """An error that means the connection is gone, not that the SQL was wrong."""
        if self.dialect == "sqlite":
            return isinstance(exc, sqlite3.OperationalError) and "locked" not in str(exc)
        psycopg = self._driver()
        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))

    def drop_schema(self) -> None:
        if self.dialect != "postgres" or self.schema is None:
            return
        psycopg = self._driver()
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    # -- the shim -----------------------------------------------------------------

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.dialect == "postgres" else text

    def ddl(self, text: str) -> str:
        if self.dialect == "postgres":
            subs = {"{{ID}}": "BIGSERIAL PRIMARY KEY", "{{JSON}}": "JSONB", "{{REAL}}": "DOUBLE PRECISION"}
        else:
            subs = {"{{ID}}": "INTEGER PRIMARY KEY AUTOINCREMENT", "{{JSON}}": "TEXT", "{{REAL}}": "REAL"}
        for token, value in subs.items():
            text = text.replace(token, value)
        return text

    def json_param(self) -> str:
        """The placeholder for a value going into a JSON column."""
        return "CAST(? AS JSONB)" if self.dialect == "postgres" else "?"

    def floor_expr(self, expr: str) -> str:
        """floor() of a non-negative expression, as an integer."""
        return f"FLOOR({expr})" if self.dialect == "postgres" else f"CAST({expr} AS INTEGER)"

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), default=str)

    @staticmethod
    def loads(value: Any) -> Any:
        """A JSON column as Python: psycopg hands JSONB back decoded, SQLite as text."""
        if value is None or not isinstance(value, (str, bytes)):
            return value
        return json.loads(value)
