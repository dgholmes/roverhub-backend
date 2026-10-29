"""runs/reconcile.py: a bridge restart fails the runs the old process was driving."""
from runs.reconcile import reconcile_boot
from runs.store import RunStore
from shared.schemas.runs import MissionRun
from shared.schemas.tasks import TaskOccurrence
from tasks.store import TaskStore
from tests.test_runs import _run

NOW = "2026-10-29T10:00:00Z"


def _stores(tmp_path):
    return RunStore(str(tmp_path / "runs")), TaskStore(str(tmp_path / "tasks"))


def _put(store, run_id, **extra):
    store.put(MissionRun.model_validate(_run(run_id=run_id, **extra)))


def test_changed_boot_id_fails_running_runs_of_that_robot_only(tmp_path):
    runs, tasks = _stores(tmp_path)
    _put(runs, "r-mine", bridge_boot_id="boot-a")
    _put(runs, "r-other-robot", bridge_boot_id="boot-a", robot_id="rvr-02")
    _put(runs, "r-done", bridge_boot_id="boot-a", result="completed")
    tasks.put_occurrence(TaskOccurrence(
        occurrence_id="r-mine", task_id="t-1", mission_id="m-1", site_id="site-a",
        robot_id="rvr-01", due_at="2026-10-29T09:00:00Z", status="running", run_id="r-mine"))

    changed = reconcile_boot(runs, tasks, "rvr-01", "boot-b", NOW)

    assert changed == ["r-mine"]
    run = runs.get("r-mine")
    assert (run.result, run.ended_at, run.end_reason) == ("failed", NOW, "bridgeRestarted")
    assert tasks.get_occurrence("r-mine").status == "failed"
    assert runs.get("r-other-robot").result == "running"
    assert runs.get("r-done").result == "completed"


def test_same_boot_id_never_fails_a_running_run(tmp_path):
    runs, tasks = _stores(tmp_path)
    _put(runs, "r-1", bridge_boot_id="boot-a")

    assert reconcile_boot(runs, tasks, "rvr-01", "boot-a", NOW) == []
    assert runs.get("r-1").result == "running"


def test_run_without_boot_id_is_left_alone(tmp_path):
    runs, tasks = _stores(tmp_path)
    _put(runs, "r-browser")  # a legacy browser-driven run records no boot id

    assert reconcile_boot(runs, tasks, "rvr-01", "boot-b", NOW) == []
    assert runs.get("r-browser").result == "running"


def test_unknown_new_boot_id_changes_nothing(tmp_path):
    runs, tasks = _stores(tmp_path)
    _put(runs, "r-1", bridge_boot_id="boot-a")

    assert reconcile_boot(runs, tasks, "rvr-01", None, NOW) == []
