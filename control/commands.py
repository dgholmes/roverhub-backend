from __future__ import annotations

import logging
from typing import Dict, Optional

from shared.schemas.commands import Command

logger = logging.getLogger("roverhub.commands")

# What the backend decided when it sent a command. An ack is the bridge's
# account of the command's progress, so these come from what was sent, not
# from the ack's body: a bridge cannot re-attribute a command (initiated_by),
# turn it into another type, or point a START_MISSION's run_id elsewhere.
_SERVER_OWNED = ("type", "label", "params", "initiated_by", "ttl_s")


class CommandStore:
    """In-memory tracking of dispatched commands and their ACK stages --
    no database this round."""

    def __init__(self):
        self._commands: Dict[str, Command] = {}

    def add(self, command: Command) -> None:
        self._commands[command.command_id] = command

    def apply_ack(self, command: Command) -> Optional[Command]:
        """Store an ack and return the command as it now stands, or None if the
        ack is refused: one for a command this backend sent to another robot.
        The ack's robot has already been checked against its topic
        (realtime/mqtt_client.py). A command this process never sent (there
        was a restart since) is stored as acked."""
        stored = self._commands.get(command.command_id)
        if stored is not None:
            if stored.robot_id != command.robot_id:
                logger.warning(
                    "ignoring an ack from %s for command %s, which was sent to %s",
                    command.robot_id, command.command_id, stored.robot_id)
                return None
            command = command.model_copy(update={f: getattr(stored, f) for f in _SERVER_OWNED})
        self._commands[command.command_id] = command
        return command

    def get(self, command_id: str) -> Optional[Command]:
        return self._commands.get(command_id)
