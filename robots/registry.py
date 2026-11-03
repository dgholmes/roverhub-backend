from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Literal, Optional

# Device acceptance (2026-10-15). INFORMATIONAL ONLY -- read this before
# building anything on it.
#
# The console wants to show "new device waiting to be accepted". This records
# that notion, and deliberately does not enforce it:
#
# - It cannot be a security control. There is no auth anywhere in RoverHub
#   (CLAUDE.md, OD-7): anyone who can reach this API can accept a device, and
#   anyone who can reach the broker can publish as any bridge. An accept step
#   gated by nothing proves nothing about who a device is.
# - It must not stop robots working. Commands, telemetry and snapshots do not
#   consult it. Gating commands on it would mean a backend restart -- which
#   forgets everything, this registry being in memory -- could leave every
#   robot uncommandable until someone re-clicked Accept, possibly mid-incident.
#   E-Stop in particular must never depend on bookkeeping like this.
#
# The default mode is therefore "auto": every bridge that registers is accepted
# on first sight, exactly as before this existed. "Already seen" cannot be the
# rule on its own because this registry is in memory: after a restart every
# robot is unseen, so a manual-by-default mode would flip the whole fleet to
# "pending" every time the backend restarts. Set DEVICE_ACCEPTANCE=manual to
# make unknown devices arrive as "pending" (and ACCEPTED_DEVICES to pre-accept
# known robot or bridge ids, which survives restarts because it is config).
# Revisit once a database (for a durable accepted set) and auth (for who may
# accept) exist; only then can this become a gate.
Acceptance = Literal["accepted", "pending", "rejected"]
AcceptanceMode = Literal["auto", "manual"]
# How the current acceptance state was reached:
#   auto     -- DEVICE_ACCEPTANCE=auto accepted it on first registration
#   config   -- its robot_id or bridge_id is listed in ACCEPTED_DEVICES
#   operator -- set via POST /robots/{id}/accept or /reject
#   None     -- pending, nobody has decided yet
AcceptanceSource = Literal["auto", "config", "operator"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# A robot is offline once nothing has been heard from it for this long. Every
# bridge message refreshes last_seen -- registration and heartbeat every
# HEARTBEAT_INTERVAL_S (5 s by default on the edge), telemetry at 10 Hz -- so
# 15 s is three heartbeat intervals: two lost heartbeats are tolerated, a third
# is not. The bridge sets no MQTT last will, so silence is the only signal a
# dead bridge, a dead link or a dead broker connection gives.
DEFAULT_OFFLINE_AFTER_S = 15.0


def offline_after_s_from_env() -> Optional[float]:
    """ROBOT_OFFLINE_AFTER_S, in seconds. 0 (or less) disables aging, which
    restores the old never-offline behaviour; a non-number refuses to start
    rather than silently picking a value."""
    raw = os.environ.get("ROBOT_OFFLINE_AFTER_S", "").strip()
    if not raw:
        return DEFAULT_OFFLINE_AFTER_S
    value = float(raw)
    return value if value > 0 else None


@dataclass
class RobotRecord:
    robot_id: str
    site_id: str
    robot_type: str
    name: str
    last_seen: Optional[datetime] = None
    online: bool = False
    abstract_state: Optional[str] = None
    sdk_state: Optional[str] = None
    bridge_id: Optional[str] = None
    schema_version: Optional[str] = None
    first_seen: Optional[datetime] = None
    # EdgeHostInfo / EdgeHostMetrics as validated dicts (shared/schemas/
    # bridge_status.py); None from a bridge that predates host reporting.
    host: Optional[dict] = None
    host_metrics: Optional[dict] = None
    host_metrics_at: Optional[str] = None
    acceptance: Acceptance = "accepted"
    acceptance_source: Optional[AcceptanceSource] = None
    acceptance_changed_at: Optional[datetime] = None
    # From /state (schema 0.0.11). active_run is an ActiveRun as a plain dict.
    control_owner: Optional[str] = None
    active_run: Optional[dict] = None
    bridge_boot_id: Optional[str] = None


class RobotRegistry:
    """In-memory registry of known robots -- no database this round. State
    is lost on process restart; that's an accepted limitation, not a bug."""

    def __init__(
        self,
        acceptance_mode: AcceptanceMode = "auto",
        preaccepted: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        offline_after_s: Optional[float] = DEFAULT_OFFLINE_AFTER_S,
    ):
        if acceptance_mode not in ("auto", "manual"):
            raise ValueError(f"acceptance_mode must be 'auto' or 'manual', not {acceptance_mode!r}")
        self._robots: Dict[str, RobotRecord] = {}
        self._mode = acceptance_mode
        self._preaccepted = {item.strip() for item in preaccepted if item and item.strip()}
        self._clock = clock
        # None (or 0) disables aging. Aging is applied when a record is read,
        # not by a background sweep: every reader -- dispatch, the scheduler,
        # the reaper, GET /robots -- goes through get()/list_all(), so they all
        # see the same answer at the same moment, with no task to start and no
        # sweep interval adding latency.
        self._offline_after = (
            timedelta(seconds=offline_after_s) if offline_after_s and offline_after_s > 0 else None
        )
        # paho's network thread writes; FastAPI's threadpool reads and decides.
        self._lock = threading.RLock()

    @property
    def acceptance_mode(self) -> AcceptanceMode:
        return self._mode

    def _initial_acceptance(self, robot_id: str, bridge_id: Optional[str]):
        if robot_id in self._preaccepted or (bridge_id and bridge_id in self._preaccepted):
            return "accepted", "config"
        if self._mode == "auto":
            return "accepted", "auto"
        return "pending", None

    def upsert_from_registration(
        self,
        robot_id: str,
        site_id: str,
        robot_type: str,
        bridge_id: Optional[str] = None,
        schema_version: Optional[str] = None,
        host: Optional[dict] = None,
    ) -> None:
        now = self._clock()
        with self._lock:
            existing = self._robots.get(robot_id)
            if existing is None:
                acceptance, source = self._initial_acceptance(robot_id, bridge_id)
                self._robots[robot_id] = RobotRecord(
                    robot_id=robot_id, site_id=site_id, robot_type=robot_type,
                    name=robot_id, last_seen=now, online=True,
                    bridge_id=bridge_id, schema_version=schema_version,
                    first_seen=now, host=host,
                    acceptance=acceptance, acceptance_source=source,
                    acceptance_changed_at=now,
                )
                return
            # Registration repeats every heartbeat tick. Refresh what it
            # describes; keep what other topics own (state, metrics) and what
            # an operator decided (name, acceptance).
            existing.site_id = site_id
            existing.robot_type = robot_type
            existing.last_seen = now
            existing.online = True
            existing.bridge_id = bridge_id
            existing.schema_version = schema_version
            existing.host = host

    def mark_seen(self, robot_id: str) -> None:
        with self._lock:
            record = self._robots.get(robot_id)
            if record is not None:
                record.last_seen = self._clock()
                record.online = True

    def mark_offline(self, robot_id: str) -> None:
        with self._lock:
            record = self._robots.get(robot_id)
            if record is not None:
                record.online = False

    def update_state(
        self, robot_id: str, abstract_state: str, sdk_state: str, *,
        control_owner: Optional[str] = None, active_run: Optional[dict] = None,
        bridge_boot_id: Optional[str] = None,
    ) -> Optional[str]:
        """Returns the PREVIOUS bridge boot id, so the caller can tell a restart.
        Owner and active run are replaced each time: a state message that omits
        them means there is none, and stale ones would show a finished run."""
        with self._lock:
            record = self._robots.get(robot_id)
            if record is None:
                return None
            previous = record.bridge_boot_id
            record.abstract_state = abstract_state
            record.sdk_state = sdk_state
            record.control_owner = control_owner
            record.active_run = active_run
            record.bridge_boot_id = bridge_boot_id
            return previous

    def update_host_metrics(self, robot_id: str, metrics: Optional[dict], at: Optional[str]) -> None:
        """From each heartbeat. A heartbeat without metrics (old bridge, or a
        failed sample) clears them: stale CPU/temperature shown as current is
        worse than "not reported"."""
        with self._lock:
            record = self._robots.get(robot_id)
            if record is not None:
                record.host_metrics = metrics
                record.host_metrics_at = at if metrics is not None else None

    def set_acceptance(self, robot_id: str, acceptance: Acceptance) -> Optional[RobotRecord]:
        """Operator decision. Returns the record, or None if unknown."""
        with self._lock:
            record = self._robots.get(robot_id)
            if record is None:
                return None
            record.acceptance = acceptance
            record.acceptance_source = "operator"
            record.acceptance_changed_at = self._clock()
            return record

    def forget(self, robot_id: str) -> bool:
        """Drop the record and any decision about it. A bridge that is still
        running re-registers within one heartbeat interval (and the broker
        replays its retained registration on our next reconnect), so it
        reappears as if new -- accepted in auto mode, pending in manual."""
        with self._lock:
            return self._robots.pop(robot_id, None) is not None

    def _age(self, record: RobotRecord) -> RobotRecord:
        """Mark a record offline if it has gone quiet. Only ever clears
        online: the next registration, heartbeat or telemetry sets it again
        (mark_seen / upsert_from_registration). Caller holds the lock."""
        if (self._offline_after is not None and record.online
                and record.last_seen is not None
                and self._clock() - record.last_seen > self._offline_after):
            record.online = False
        return record

    def get(self, robot_id: str) -> Optional[RobotRecord]:
        with self._lock:
            record = self._robots.get(robot_id)
            return self._age(record) if record is not None else None

    def list_all(self) -> List[RobotRecord]:
        with self._lock:
            return [self._age(record) for record in self._robots.values()]
