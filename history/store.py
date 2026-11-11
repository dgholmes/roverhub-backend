"""The history store: the same tables and queries on SQLite and Postgres.

Writes of ingested rows come from one thread (writer.py) in batches; the
incident workflow and every read come from request threads. Each thread keeps
its own connection. The workflow runs as a compare-and-set inside one
transaction, and writes its audit row in that same transaction, so a recorded
acknowledgement always has its audit entry.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from history.db import Database, HistoryUnavailable, epoch, iso
from history.records import AuditRecord, IncidentRecord, PerceptionRecord, SampleRecord

logger = logging.getLogger("roverhub.history")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

OUTCOMES = ("confirmed", "false_alarm", "other")
TABLES = ("incidents", "telemetry_samples", "perception_status", "audit_log")
DAY_S = 86_400.0


def _migrations() -> List[Tuple[int, str, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        match = _MIGRATION_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), match.group(2), path))
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"history migrations must be numbered 0001.. with no gaps: {versions}")
    return found


LATEST_MIGRATION = _migrations()[-1][0]


def _statements(text: str) -> List[str]:
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in re.split(r";\s*(?:\n|$)", body) if s.strip()]


class IncidentNotFound(LookupError):
    pass


class IncidentConflict(Exception):
    """The action is not possible from the incident's state; `message` says why."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _cursor_out(*values) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")


def _cursor_in(cursor: Optional[str], size: int) -> Optional[list]:
    if not cursor:
        return None
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, TypeError):
        raise ValueError("cursor is not one this server gave out") from None
    if not isinstance(values, list) or len(values) != size:
        raise ValueError("cursor is not one this server gave out")
    return values


def _bound(value: Optional[str], name: str) -> Optional[float]:
    if value is None:
        return None
    moment = epoch(value)
    if moment is None:
        raise ValueError(f"{name!r} is not an ISO 8601 timestamp: {value!r}")
    return moment


class HistoryStore:
    def __init__(self, db: Database):
        self.db = db
        self._local = threading.local()
        self._opened: List[Any] = []
        self._opened_lock = threading.Lock()

    # -- connections and transactions ---------------------------------------------------

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self.db.connect()
            self._local.conn = conn
            with self._opened_lock:
                self._opened.append(conn)
        return conn

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            with self._opened_lock:
                if conn in self._opened:
                    self._opened.remove(conn)

    @contextmanager
    def transaction(self, write: bool = False):
        conn = self._conn()
        try:
            # IMMEDIATE takes SQLite's write lock up front, so a read-then-update
            # cannot be overtaken by another writer between the two.
            conn.execute("BEGIN IMMEDIATE" if write and self.db.dialect == "sqlite" else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            if isinstance(exc, Exception) and self.db.is_disconnect(exc):
                self._drop()
            raise

    def _exec(self, conn, sql: str, params: Sequence = ()):
        return conn.execute(self.db.sql(sql), tuple(params))

    def _rows(self, conn, sql: str, params: Sequence = ()) -> List[Dict[str, Any]]:
        cursor = self._exec(conn, sql, params)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def _read(self, sql: str, params: Sequence = ()) -> List[Dict[str, Any]]:
        with self.transaction() as conn:
            return self._rows(conn, sql, params)

    def execute_raw(self, sql: str, params: Sequence = ()) -> None:
        with self.transaction(write=True) as conn:
            self._exec(conn, sql, params)

    def close(self) -> None:
        with self._opened_lock:
            opened, self._opened = self._opened, []
        for conn in opened:
            try:
                conn.close()
            except Exception:
                pass
        self._local = threading.local()

    # -- migrations ---------------------------------------------------------------------

    def level(self) -> int:
        rows = self._read("SELECT MAX(version) AS v FROM schema_migrations")
        return int(rows[0]["v"] or 0)

    def migrate(self) -> int:
        """Apply what is missing, each migration in its own transaction, and
        return the level. Refuses a database a newer backend has migrated."""
        with self.transaction(write=True) as conn:
            self._exec(conn, self.db.ddl(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at {{REAL}} NOT NULL)"))
        current = self.level()
        if current > LATEST_MIGRATION:
            raise HistoryUnavailable(
                f"the history database ({self.db.describe()}) is at migration {current}, and this backend "
                f"knows {LATEST_MIGRATION} at most; run a backend at least as new as the one that migrated it, "
                f"or point HISTORY_DB / DATABASE_URL at another database")
        import time
        for version, name, path in _migrations():
            if version <= current:
                continue
            with self.transaction(write=True) as conn:
                for statement in _statements(path.read_text(encoding="utf-8")):
                    self._exec(conn, self.db.ddl(statement))
                self._exec(conn, "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                           (version, name, time.time()))
            logger.warning("history: applied migration %04d_%s to %s", version, name, self.db.describe())
        return self.level()

    # -- ingested rows (the writer's batches) ------------------------------------------------

    def write(self, records: Iterable[Any]) -> None:
        """Insert a batch in one transaction. An incident already stored only has
        its last_seen moved: QoS 1 redelivers, and the spool re-sends."""
        by_type: Dict[type, List[Any]] = {}
        for record in records:
            by_type.setdefault(type(record), []).append(record)
        if not by_type:
            return
        j = self.db.json_param()
        with self.transaction(write=True) as conn:
            cursor = conn.cursor()
            if IncidentRecord in by_type:
                cursor.executemany(self.db.sql(
                    "INSERT INTO incidents (incident_id, robot_id, site_id, rule_id, label, severity, opened_at, "
                    "camera, message, confidence, payload, evidence_snapshot_ids, x_m, y_m, map_id, run_id, "
                    f"received_at, last_seen, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {j}, {j}, ?, ?, ?, ?, "
                    "?, ?, 'open') ON CONFLICT (incident_id) DO UPDATE SET last_seen = excluded.last_seen"),
                    [self._incident_row(r) for r in by_type[IncidentRecord]])
            if SampleRecord in by_type:
                cursor.executemany(self.db.sql(
                    "INSERT INTO telemetry_samples (robot_id, site_id, at, battery_pct, x_m, y_m, heading_deg, "
                    "position_quality, speed_mps, abstract_state, sdk_state, posture, control_owner, "
                    "active_run_id, link, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
                    [(r.robot_id, r.site_id, r.at, r.battery_pct, r.x_m, r.y_m, r.heading_deg,
                      r.position_quality, r.speed_mps, r.abstract_state, r.sdk_state, r.posture,
                      r.control_owner, r.active_run_id, r.link, r.reason) for r in by_type[SampleRecord]])
            if PerceptionRecord in by_type:
                cursor.executemany(self.db.sql(
                    "INSERT INTO perception_status (robot_id, site_id, at, source_state, fps, camera, detectors, "
                    f"payload) VALUES (?, ?, ?, ?, ?, ?, {j}, {j})"),
                    [(r.robot_id, r.site_id, r.at, r.source_state, r.fps, r.camera,
                      self.db.dumps(r.detectors), self.db.dumps(r.payload)) for r in by_type[PerceptionRecord]])
            if AuditRecord in by_type:
                cursor.executemany(self.db.sql(
                    "INSERT INTO audit_log (at, actor, action, target, outcome, site_id, detail) "
                    f"VALUES (?, ?, ?, ?, ?, ?, {j})"),
                    [self._audit_row(r) for r in by_type[AuditRecord]])

    def _incident_row(self, r: IncidentRecord) -> tuple:
        p = r.payload
        detections = p.get("detections") or []
        confidences = [d.get("confidence") for d in detections if isinstance(d, dict)
                       and isinstance(d.get("confidence"), (int, float))]
        # Map position and run id when the payload carries them (perception
        # does not yet): either a `position` object or top-level fields.
        position = p.get("position") if isinstance(p.get("position"), dict) else p
        x_m, y_m = position.get("x_m"), position.get("y_m")
        map_id = position.get("map_id") or p.get("map_id")
        evidence = [s for s in (p.get("evidence_snapshot_ids") or []) if isinstance(s, str)]
        return (
            p["incident_id"], r.robot_id, r.site_id, p.get("rule_id"), p["label"], p["severity"],
            epoch(p.get("opened_at")) or r.received_at, p.get("camera"), p.get("message"),
            max(confidences) if confidences else None, self.db.dumps(p), self.db.dumps(evidence),
            x_m if isinstance(x_m, (int, float)) else None, y_m if isinstance(y_m, (int, float)) else None,
            map_id if isinstance(map_id, str) else None,
            p.get("run_id") if isinstance(p.get("run_id"), str) else None,
            r.received_at, r.received_at,
        )

    def _audit_row(self, r: AuditRecord) -> tuple:
        return (r.at, r.actor, r.action, r.target, r.outcome, r.site_id, self.db.dumps(r.detail or {}))

    # -- incidents ------------------------------------------------------------------------

    _INCIDENT_COLUMNS = (
        "incident_id, robot_id, site_id, rule_id, label, severity, opened_at, camera, message, confidence, "
        "payload, evidence_snapshot_ids, x_m, y_m, map_id, run_id, received_at, last_seen, state, "
        "acknowledged_by, acknowledged_at, resolved_by, resolved_at, outcome, note")

    def _incident_out(self, row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        for key in ("opened_at", "received_at", "last_seen", "acknowledged_at", "resolved_at"):
            out[key] = iso(row[key])
        out["payload"] = self.db.loads(row["payload"])
        out["evidence_snapshot_ids"] = self.db.loads(row["evidence_snapshot_ids"]) or []
        return out

    def get_incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        rows = self._read(f"SELECT {self._INCIDENT_COLUMNS} FROM incidents WHERE incident_id = ?", (incident_id,))
        return self._incident_out(rows[0]) if rows else None

    def site_of_incident(self, incident_id: str) -> Optional[Tuple[str, str]]:
        rows = self._read("SELECT site_id, robot_id FROM incidents WHERE incident_id = ?", (incident_id,))
        return (rows[0]["site_id"], rows[0]["robot_id"]) if rows else None

    def list_incidents(self, site_id=None, robot_id=None, state=None, kind=None, since=None, until=None,
                       limit: int = 50, cursor: Optional[str] = None):
        where, params = [], []
        for column, value in (("site_id", site_id), ("robot_id", robot_id), ("state", state), ("label", kind)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        lo, hi = _bound(since, "from"), _bound(until, "to")
        if lo is not None:
            where.append("opened_at >= ?")
            params.append(lo)
        if hi is not None:
            where.append("opened_at < ?")
            params.append(hi)
        after = _cursor_in(cursor, 2)
        if after is not None:
            where.append("(opened_at < ? OR (opened_at = ? AND incident_id < ?))")
            params += [after[0], after[0], after[1]]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._read(
            f"SELECT {self._INCIDENT_COLUMNS} FROM incidents {clause} "
            f"ORDER BY opened_at DESC, incident_id DESC LIMIT ?", params + [limit + 1])
        page = rows[:limit]
        next_cursor = _cursor_out(page[-1]["opened_at"], page[-1]["incident_id"]) if len(rows) > limit else None
        return [self._incident_out(r) for r in page], next_cursor

    def _locked_incident(self, conn, incident_id: str) -> Dict[str, Any]:
        lock = " FOR UPDATE" if self.db.dialect == "postgres" else ""
        rows = self._rows(conn, f"SELECT {self._INCIDENT_COLUMNS} FROM incidents WHERE incident_id = ?{lock}",
                          (incident_id,))
        if not rows:
            raise IncidentNotFound(incident_id)
        return rows[0]

    def _audit_in(self, conn, at: float, actor: str, action: str, target: str, outcome: str,
                  site_id: Optional[str], detail: Dict[str, Any]) -> None:
        self._exec(conn, "INSERT INTO audit_log (at, actor, action, target, outcome, site_id, detail) "
                         f"VALUES (?, ?, ?, ?, ?, ?, {self.db.json_param()})",
                   self._audit_row(AuditRecord(at, actor, action, target, outcome, detail, site_id)))

    def acknowledge(self, incident_id: str, actor: str, now: float) -> Dict[str, Any]:
        with self.transaction(write=True) as conn:
            row = self._locked_incident(conn, incident_id)
            if row["state"] == "resolved":
                raise IncidentConflict(f"incident {incident_id} is already resolved; it cannot be acknowledged again")
            if row["state"] == "open":
                self._exec(conn, "UPDATE incidents SET state = 'acknowledged', acknowledged_by = ?, "
                                 "acknowledged_at = ? WHERE incident_id = ?", (actor, now, incident_id))
                self._audit_in(conn, now, actor, "incident.acknowledge", incident_id, "ok", row["site_id"],
                               {"from": "open"})
        return self.get_incident(incident_id)

    def resolve(self, incident_id: str, actor: str, outcome: str, note: Optional[str], now: float) -> Dict[str, Any]:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        with self.transaction(write=True) as conn:
            row = self._locked_incident(conn, incident_id)
            if row["state"] == "resolved":
                if row["outcome"] == outcome and row["note"] == note:
                    return self._incident_out(row)
                raise IncidentConflict(
                    f"incident {incident_id} was already resolved as {row['outcome']} by {row['resolved_by']}; "
                    "it cannot be resolved again")
            if row["state"] == "open":
                # Resolving straight from open is allowed, and says who saw it.
                self._exec(conn, "UPDATE incidents SET acknowledged_by = ?, acknowledged_at = ? "
                                 "WHERE incident_id = ?", (actor, now, incident_id))
            self._exec(conn, "UPDATE incidents SET state = 'resolved', resolved_by = ?, resolved_at = ?, "
                             "outcome = ?, note = ? WHERE incident_id = ?",
                       (actor, now, outcome, note, incident_id))
            self._audit_in(conn, now, actor, "incident.resolve", incident_id, outcome, row["site_id"],
                           {"from": row["state"], "note": note})
        return self.get_incident(incident_id)

    # -- telemetry history ----------------------------------------------------------------------

    _SAMPLE_COLUMNS = ("at, battery_pct, x_m, y_m, heading_deg, position_quality, speed_mps, abstract_state, "
                       "sdk_state, posture, control_owner, active_run_id, link, reason")

    def history(self, robot_id: str, since: float, until: float, max_points: int) -> List[Dict[str, Any]]:
        """Samples with since <= at < until, oldest first, at most max_points:
        past that the window is cut into max_points equal buckets and the first
        sample of each is returned, so the thinning happens in the database."""
        args = (robot_id, since, until)
        count = self._read("SELECT COUNT(*) AS n FROM telemetry_samples WHERE robot_id = ? AND at >= ? AND at < ?",
                           args)[0]["n"]
        if count <= max_points:
            rows = self._read(f"SELECT {self._SAMPLE_COLUMNS} FROM telemetry_samples "
                              "WHERE robot_id = ? AND at >= ? AND at < ? ORDER BY at, id", args)
        else:
            width = (until - since) / max_points
            bucket = self.db.floor_expr("(at - ?) / ?")
            rows = self._read(
                f"SELECT {self._SAMPLE_COLUMNS} FROM telemetry_samples WHERE id IN ("
                f"SELECT MIN(id) FROM telemetry_samples WHERE robot_id = ? AND at >= ? AND at < ? "
                f"GROUP BY {bucket}) ORDER BY at, id", (robot_id, since, until, since, width))
        return [{**r, "at": iso(r["at"])} for r in rows]

    def has_samples(self, robot_id: str) -> bool:
        return bool(self._read("SELECT 1 AS one FROM telemetry_samples WHERE robot_id = ? LIMIT 1", (robot_id,)))

    def latest_perception(self) -> List[Dict[str, Any]]:
        rows = self._read("SELECT robot_id, site_id, at, source_state, fps, camera, detectors FROM perception_status "
                          "WHERE id IN (SELECT MAX(id) FROM perception_status GROUP BY robot_id) ORDER BY robot_id")
        return [{**r, "at": iso(r["at"]), "detectors": self.db.loads(r["detectors"])} for r in rows]

    # -- audit ------------------------------------------------------------------------------------

    def list_audit(self, actor=None, action=None, target=None, since=None, until=None,
                   limit: int = 50, cursor: Optional[str] = None):
        where, params = [], []
        for column, value in (("actor", actor), ("action", action), ("target", target)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        lo, hi = _bound(since, "from"), _bound(until, "to")
        if lo is not None:
            where.append("at >= ?")
            params.append(lo)
        if hi is not None:
            where.append("at < ?")
            params.append(hi)
        after = _cursor_in(cursor, 2)
        if after is not None:
            where.append("(at < ? OR (at = ? AND id < ?))")
            params += [after[0], after[0], after[1]]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._read(f"SELECT id, at, actor, action, target, outcome, site_id, detail FROM audit_log {clause} "
                          "ORDER BY at DESC, id DESC LIMIT ?", params + [limit + 1])
        page = rows[:limit]
        next_cursor = _cursor_out(page[-1]["at"], page[-1]["id"]) if len(rows) > limit else None
        return [{**r, "at": iso(r["at"]), "detail": self.db.loads(r["detail"]) or {}} for r in page], next_cursor

    # -- housekeeping -------------------------------------------------------------------------------

    def prune(self, now: float, retention_days: float) -> Dict[str, int]:
        """Delete telemetry and perception-status rows older than the retention.
        The newest status row of each robot is kept however old, because it is
        still that robot's latest. Incidents and audit rows are never pruned."""
        cutoff = now - retention_days * DAY_S
        with self.transaction(write=True) as conn:
            telemetry = self._exec(conn, "DELETE FROM telemetry_samples WHERE at < ?", (cutoff,)).rowcount
            perception = self._exec(
                conn, "DELETE FROM perception_status WHERE at < ? AND id NOT IN "
                      "(SELECT MAX(id) FROM perception_status GROUP BY robot_id)", (cutoff,)).rowcount
        return {"telemetry_samples": telemetry, "perception_status": perception}

    def counts(self) -> Dict[str, int]:
        with self.transaction() as conn:
            return {t: int(self._rows(conn, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]) for t in TABLES}
