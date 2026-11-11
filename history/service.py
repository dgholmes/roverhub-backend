"""History as the app sees it: one object holding the store, the writer, the
decimators and the audit recorder, built from the environment.

Building it never raises. If history cannot start -- a HISTORY_DB in a folder
that does not exist, an unreachable or wrong DATABASE_URL, psycopg missing, a
database migrated by a newer backend -- the backend still starts, the reason is
logged loudly, every history route answers 503 with it, and everything that
would have recorded something does nothing.

Environment:
    HISTORY_DB                    SQLite file (default data/history.sqlite3)
    DATABASE_URL                  postgres://... switches to Postgres
    HISTORY_TELEMETRY_INTERVAL_S  one telemetry row per robot per this (default 5)
    HISTORY_RETENTION_DAYS        telemetry/perception rows kept this long (default 30)
    HISTORY_QUEUE_MAX             rows held in memory before dropping (default 20000)
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from history.audit import AuditRecorder
from history.db import Database, HistoryUnavailable
from history.ingest import PerceptionTracker, TelemetryDecimator, incident_record
from history.store import HistoryStore
from history.writer import HistoryWriter

logger = logging.getLogger("roverhub.history")

DEFAULT_HISTORY_DB = str(Path(__file__).resolve().parents[1] / "data" / "history.sqlite3")


def _float_env(env, name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not value > 0:
        raise ValueError(f"{name} must be a number above 0, got {raw!r}")
    return value


class History:
    def __init__(self, store: Optional[HistoryStore] = None, engine: Optional[str] = None,
                 error: Optional[str] = None, interval_s: float = 5.0, retention_days: float = 30.0,
                 max_queue: int = 20_000, clock: Callable[[], float] = time.time):
        self.store = store
        self.engine = engine
        self.error = error
        self.clock = clock
        self.writer = HistoryWriter(store, max_queue=max_queue, retention_days=retention_days, clock=clock) \
            if store is not None else None
        self.decimator = TelemetryDecimator(interval_s, clock)
        self.perception = PerceptionTracker(clock)
        self.audit = AuditRecorder(self.submit, clock)

    @property
    def available(self) -> bool:
        return self.store is not None

    @classmethod
    def unavailable(cls, reason: str) -> "History":
        return cls(error=reason)

    @classmethod
    def from_env(cls, env=os.environ) -> "History":
        try:
            db = Database.from_env(env, DEFAULT_HISTORY_DB)
            interval = _float_env(env, "HISTORY_TELEMETRY_INTERVAL_S", 5.0)
            retention = _float_env(env, "HISTORY_RETENTION_DAYS", 30.0)
            max_queue = int(_float_env(env, "HISTORY_QUEUE_MAX", 20_000))
        except ValueError as exc:
            logger.error("HISTORY IS OFF: %s", exc)
            return cls.unavailable(str(exc))
        try:
            store = HistoryStore(db)
            level = store.migrate()
        except HistoryUnavailable as exc:
            logger.error("HISTORY IS OFF: %s", exc.message)
            return cls.unavailable(exc.message)
        except Exception as exc:  # an unreachable server, a file that is not a database, ...
            reason = f"could not open {db.describe()}: {type(exc).__name__}: {exc}"
            logger.error("HISTORY IS OFF: %s", reason)
            return cls.unavailable(reason)
        logger.warning("history: %s at %s, migration %d", db.dialect, db.describe(), level)
        return cls(store, db.dialect, interval_s=interval, retention_days=retention, max_queue=max_queue)

    # -- writing ---------------------------------------------------------------------

    def submit(self, record) -> None:
        if self.writer is not None:
            self.writer.submit(record)

    def observe(self, kind: str, site_id: str, robot_id: str, data: Dict[str, Any]) -> None:
        """BackendMqttClient's hook, on paho's thread: decide and queue, never
        raise, never touch the database."""
        if self.writer is None or not isinstance(data, dict):
            return
        try:
            if kind == "telemetry":
                record = self.decimator.on_telemetry(site_id, robot_id, data)
            elif kind == "state":
                record = self.decimator.on_state(site_id, robot_id, data)
            elif kind == "perception_status":
                record = self.perception.on_status(site_id, robot_id, data)
            elif kind == "incident":
                record = incident_record(site_id, robot_id, data, self.clock())
            else:
                return
            if record is not None:
                self.writer.submit(record)
        except Exception:
            logger.exception("history: could not take a %s message from %s", kind, robot_id)

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if self.writer is not None:
            self.writer.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        if self.writer is not None:
            self.writer.stop(timeout_s)

    def flush(self, timeout_s: float = 5.0) -> bool:
        return self.writer.flush(timeout_s) if self.writer is not None else True

    def status(self) -> Dict[str, Any]:
        stats = self.writer.stats()
        return {
            "available": True, "engine": self.engine, "migration": self.store.level(),
            "rows": self.store.counts(), "queue_depth": stats["queue_depth"], "dropped": stats["dropped"],
            "written": stats["written"], "write_errors": stats["write_errors"],
            "connected": stats["connected"], "last_error": stats["last_error"],
            "last_write_at": stats["last_write_at"],
        }
