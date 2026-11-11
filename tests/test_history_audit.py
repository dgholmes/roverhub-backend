"""history/audit.py: who did what, attributed from the server side."""
from shared.schemas.commands import Command
from shared.schemas.missions import MissionLibraryEntry
from shared.schemas.runs import MissionRun

from history.audit import AuditRecorder, bind_actor, current_actor, reset_actor

T0 = 1_762_840_800.0


def command(stage="requested", initiated_by="mallory", type_="SET_STATE", reason=None):
    return Command(command_id="c-1", robot_id="rover-a", type=type_, label=type_, initiated_by=initiated_by,
                   current_stage=stage, failure_reason=reason)


def recorder():
    rows = []
    return AuditRecorder(rows.append, clock=lambda: T0), rows


def test_a_published_command_is_attributed_to_the_signed_in_actor_not_the_body():
    audit, rows = recorder()
    token = bind_actor("ana")
    try:
        audit.command_published("rover-a", command(initiated_by="mallory"), site_id="site-a")
    finally:
        reset_actor(token)
    (row,) = rows
    assert (row.actor, row.action, row.target, row.outcome, row.site_id) == (
        "ana", "command.publish", "rover-a", "sent", "site-a")
    assert row.detail["type"] == "SET_STATE" and row.detail["command_id"] == "c-1"
    assert row.detail["initiated_by"] == "mallory"   # what the body claimed, kept as a fact
    assert current_actor() is None


def test_outside_a_request_the_command_names_its_own_origin():
    audit, rows = recorder()
    audit.command_published("rover-a", command(initiated_by="schedule", type_="START_MISSION"))
    assert rows[0].actor == "schedule"


def test_a_command_that_could_not_be_sent_says_so():
    audit, rows = recorder()
    audit.command_published("rover-a", command(), error="could not publish command to the broker (rc=4)")
    assert rows[0].outcome == "not_sent" and "rc=4" in rows[0].detail["error"]


def test_the_final_ack_is_recorded_once_with_the_commanders_actor():
    audit, rows = recorder()
    token = bind_actor("ana")
    audit.command_published("rover-a", command())
    reset_actor(token)
    audit.command_acked(command(stage="bridge_received"))          # not final
    audit.command_acked(command(stage="execution_failed", reason="REJECTED_NOT_CONTROL_OWNER"))
    audit.command_acked(command(stage="execution_failed"))          # redelivered: nothing new
    outcomes = [r for r in rows if r.action == "command.outcome"]
    assert len(outcomes) == 1
    assert (outcomes[0].actor, outcomes[0].outcome) == ("ana", "execution_failed")
    assert outcomes[0].detail["failure_reason"] == "REJECTED_NOT_CONTROL_OWNER"


def _run(result="running", end_reason=None, started_by="ana"):
    mission = MissionLibraryEntry(mission_id="m-1", name="Patrol", type="patrol", kind="program",
                                  creator="x", updated_at="t", map_id="map-1", program_state="{}")
    return MissionRun(run_id="r-1", mission_id="m-1", mission=mission, robot_id="rover-a", site_id="site-a",
                      started_at="t", started_by=started_by, result=result, end_reason=end_reason)


def test_run_start_and_end_are_recorded():
    audit, rows = recorder()
    audit.run_changed(None, _run())
    audit.run_changed(_run(), _run())                                  # no transition
    audit.run_changed(_run(), _run("aborted", "stopped"))
    assert [(r.action, r.target, r.outcome, r.actor) for r in rows] == [
        ("run.start", "r-1", "running", "ana"),
        ("run.end", "r-1", "aborted", "system"),
    ]
    assert rows[1].detail["end_reason"] == "stopped"


def test_sign_in_attempts_are_recorded():
    audit, rows = recorder()
    audit.login("ana", True)
    audit.login("eve", False)
    assert [(r.actor, r.action, r.outcome) for r in rows] == [
        ("ana", "auth.login", "ok"), ("eve", "auth.login", "refused")]


def test_a_failing_sink_never_reaches_the_caller():
    def explode(_):
        raise RuntimeError("queue gone")
    audit = AuditRecorder(explode, clock=lambda: T0)
    audit.command_published("rover-a", command())
    audit.record("map.status", "map-1", "active")
