"""Who did what: the audit log's producers.

The actor is decided on the server. A request binds it from the signed-in
principal (api/app.py's `_bind_audit_actor`, an app-wide dependency next to the
auth guard): the user's id, a device's robot id, or `operator-dev` when sign-in
is off. Nothing a request body says ("initiated_by", "started_by") is ever the
actor; it is kept in `detail` as what was claimed. Work with no request behind it
-- the scheduler, an MQTT callback -- falls back to the origin the record itself
names (`schedule`) or `system`.

Every method here hands a record to the writer and returns; none raises.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar, Token
from typing import Any, Callable, Dict, Optional

from history.records import AuditRecord

logger = logging.getLogger("roverhub.history")

_ACTOR: ContextVar[Optional[str]] = ContextVar("roverhub_audit_actor", default=None)

# Stages after which no further ack changes the outcome.
FINAL_STAGES = frozenset({"execution_completed", "execution_failed", "timed_out", "cancelled"})
# How many published commands are remembered until their final ack.
MAX_PENDING = 4096


def bind_actor(actor: Optional[str]) -> Token:
    return _ACTOR.set(actor)


def reset_actor(token: Token) -> None:
    _ACTOR.reset(token)


def current_actor() -> Optional[str]:
    return _ACTOR.get()


class AuditRecorder:
    def __init__(self, submit: Callable[[AuditRecord], None], clock: Callable[[], float] = time.time):
        self._submit = submit
        self._clock = clock
        self._lock = threading.Lock()
        # command_id -> (actor, robot_id, site_id), until its final ack.
        self._pending: "OrderedDict[str, tuple]" = OrderedDict()

    def record(self, action: str, target: Optional[str], outcome: Optional[str],
               detail: Optional[Dict[str, Any]] = None, actor: Optional[str] = None,
               site_id: Optional[str] = None, fallback: str = "system") -> None:
        try:
            self._submit(AuditRecord(at=self._clock(), actor=actor or current_actor() or fallback, action=action,
                                     target=target, outcome=outcome, detail=detail or {}, site_id=site_id))
        except Exception:
            logger.exception("could not queue the audit row for %s %s", action, target)

    # -- commands: published once, settled by their final ack ---------------------------

    def command_published(self, robot_id: str, command, error: Optional[str] = None,
                          site_id: Optional[str] = None) -> None:
        actor = current_actor() or command.initiated_by or "system"
        detail = {"command_id": command.command_id, "type": command.type, "params": command.params,
                  "initiated_by": command.initiated_by}
        if error is not None:
            detail["error"] = error
        else:
            with self._lock:
                self._pending[command.command_id] = (actor, robot_id, site_id)
                while len(self._pending) > MAX_PENDING:
                    self._pending.popitem(last=False)
        self.record("command.publish", robot_id, "not_sent" if error else "sent", detail,
                    actor=actor, site_id=site_id)

    def command_acked(self, command) -> None:
        if command.current_stage not in FINAL_STAGES:
            return
        with self._lock:
            pending = self._pending.pop(command.command_id, None)
        if pending is None:
            return  # a redelivered final ack, or a command this process never sent
        actor, robot_id, site_id = pending
        self.record("command.outcome", robot_id, command.current_stage,
                    {"command_id": command.command_id, "type": command.type,
                     "failure_reason": command.failure_reason}, actor=actor, site_id=site_id)

    # -- runs, maps, sign-in -------------------------------------------------------------

    def run_changed(self, previous, run) -> None:
        """RunStore's observer: every write of a run passes here."""
        if previous is None:
            self.record("run.start", run.run_id, run.result,
                        {"robot_id": run.robot_id, "mission_id": run.mission_id, "source": run.source,
                         "started_by": run.started_by},
                        site_id=run.site_id, fallback=run.started_by or "system")
        elif previous.result == "running" and run.result != "running":
            self.record("run.end", run.run_id, run.result,
                        {"robot_id": run.robot_id, "mission_id": run.mission_id, "end_reason": run.end_reason},
                        site_id=run.site_id)

    def map_status(self, map_id: str, status: str, site_id: Optional[str] = None) -> None:
        self.record("map.status", map_id, status, {"status": status}, site_id=site_id)

    def login(self, username: str, ok: bool, user_id: Optional[str] = None) -> None:
        self.record("auth.login", username, "ok" if ok else "refused", {"username": username},
                    actor=user_id or username)
