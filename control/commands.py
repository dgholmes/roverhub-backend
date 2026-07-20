from __future__ import annotations

from typing import Dict, Optional

from shared.schemas.commands import Command


class CommandStore:
    """In-memory tracking of dispatched commands and their ACK stages --
    no database this round."""

    def __init__(self):
        self._commands: Dict[str, Command] = {}

    def add(self, command: Command) -> None:
        self._commands[command.command_id] = command

    def apply_ack(self, command: Command) -> None:
        self._commands[command.command_id] = command

    def get(self, command_id: str) -> Optional[Command]:
        return self._commands.get(command_id)
