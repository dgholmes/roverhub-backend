from control.commands import CommandStore
from shared.schemas.commands import Command


def test_add_and_get_round_trip():
    store = CommandStore()
    command = Command(command_id="cmd-1", robot_id="robot-dev-01", type="ESTOP", label="E-Stop", initiated_by="operator-1")
    store.add(command)
    assert store.get("cmd-1") is command


def test_apply_ack_replaces_stored_command():
    store = CommandStore()
    command = Command(command_id="cmd-1", robot_id="robot-dev-01", type="ESTOP", label="E-Stop", initiated_by="operator-1")
    store.add(command)
    updated = command.model_copy(update={"current_stage": "execution_completed"})
    store.apply_ack(updated)
    assert store.get("cmd-1").current_stage == "execution_completed"


def test_get_missing_returns_none():
    store = CommandStore()
    assert store.get("cmd-ghost") is None
