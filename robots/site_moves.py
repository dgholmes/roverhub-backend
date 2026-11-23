"""Robots this backend has told to move to another site (SET_SITE, schema
0.0.29), until they are heard from there.

WHY
---
A device key writes only under the site its robot registered at
(auth/mqtt_hooks.py), so a robot is never shown at a site by a message its
registration does not back. A moved bridge restarts under the new site and
writes there before the backend has its new registration: paho's online
heartbeat goes out on CONNACK, ahead of the registration. Refused, and the
broker (mosquitto-go-auth) caches the refusal for 30 s, dropping the robot's
heartbeat and retained state at the new site for that long.

So once SET_SITE is sent, the robot's key may write at the site it is moving
to as well. The allowance ends when:
  - the robot registers there (`site_of` says so: nothing left to allow);
  - the bridge refuses the move, or the command times out (`settle`);
  - SITE_MOVE_TTL_S passes (a bridge run by hand does not restart itself).
A newer SET_SITE for the same robot replaces it.

There is one organisation per backend (auth/users.py), so every site the
backend knows is the organisation's; the command's own checks (control/
router.py) are what keep a robot to sites the caller may configure.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

# How long a robot's key may write at the site it was sent to before it is
# heard from there. A bridge restarts in seconds (RestartSec=5 plus its start,
# TimeoutStartSec=90 at the very worst), so this is generous.
SITE_MOVE_TTL_S = 300.0

# The stages that end a SET_SITE without a move.
_UNMOVED_STAGES = frozenset({"execution_failed", "timed_out"})


@dataclass(frozen=True)
class _Move:
    site_id: str
    command_id: str
    until: float


class SiteMoves:
    """`site_of(robot_id)`: the site the robot is registered at (the registry),
    or None. Thread-safe: the broker's checks and the MQTT thread both read it."""

    def __init__(self, site_of: Optional[Callable[[str], Optional[str]]] = None,
                 ttl_s: float = SITE_MOVE_TTL_S, clock: Callable[[], float] = time.monotonic):
        self._site_of = site_of
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._moves: Dict[str, _Move] = {}

    def begin(self, robot_id: str, site_id: str, command_id: str) -> None:
        """SET_SITE `command_id` is about to be sent to `robot_id`."""
        with self._lock:
            self._moves[robot_id] = _Move(site_id, command_id, self._clock() + self._ttl_s)

    def cancel(self, robot_id: str, command_id: str) -> None:
        """The command never reached the broker."""
        with self._lock:
            move = self._moves.get(robot_id)
            if move is not None and move.command_id == command_id:
                del self._moves[robot_id]

    def settle(self, command) -> None:
        """An ack: a SET_SITE the bridge refused, or that timed out, ends its
        move. `restarting` (execution_completed) keeps it: the bridge is on its
        way to the new site."""
        if getattr(command, "type", None) != "SET_SITE" or command.current_stage not in _UNMOVED_STAGES:
            return
        self.cancel(command.robot_id, command.command_id)

    def target(self, robot_id: str) -> Optional[str]:
        """The site `robot_id` is moving to, or None."""
        with self._lock:
            move = self._moves.get(robot_id)
            if move is None:
                return None
            if self._clock() >= move.until:
                del self._moves[robot_id]
                return None
        if self._site_of is not None and self._site_of(robot_id) == move.site_id:
            # Registered there: the registry's site covers it from now on.
            self.cancel(robot_id, move.command_id)
            return None
        return move.site_id

    def clear(self) -> None:
        with self._lock:
            self._moves.clear()
