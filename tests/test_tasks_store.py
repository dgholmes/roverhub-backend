"""File-backed task and occurrence store: tasks/store.py."""
import os

import pytest
from fastapi import HTTPException

from shared.schemas.tasks import MissionTask, TaskOccurrence
from tasks.store import TaskStore


def _task(task_id="t-1", site_id="site-a", **extra):
    return MissionTask(
        task_id=task_id, mission_id="m-1", site_id=site_id, robot_id="robot-1",
        created_by="operator-dev", created_at="2026-10-23T00:00:00Z",
        updated_at="2026-10-23T00:00:00Z", **extra,
    )


def _occ(due_at, task_id="t-1", site_id="site-a", **extra):
    compact = due_at.replace("-", "").replace(":", "")
    return TaskOccurrence(
        occurrence_id=f"{task_id}-{compact}", task_id=task_id, mission_id="m-1",
        site_id=site_id, robot_id="robot-1", due_at=due_at, **extra,
    )


def test_put_and_get_round_trip(tmp_path):
    store = TaskStore(str(tmp_path))
    task = _task(max_duration_min=45)
    occ = _occ("2026-10-24T09:00:00Z", status="skipped", skip_reason="robot_busy", message="busy")
    store.put_task(task)
    store.put_occurrence(occ)

    assert store.get_task("t-1") == task
    assert store.get_occurrence(occ.occurrence_id) == occ
    # Two subdirectories, one JSON file per record.
    assert (tmp_path / "tasks" / "t-1.json").is_file()
    assert (tmp_path / "occurrences" / f"{occ.occurrence_id}.json").is_file()
    assert store.get_task("nope") is None
    assert store.get_occurrence("nope") is None


def test_put_replaces_and_leaves_no_temp_files(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_task(_task())
    store.put_task(_task(status="paused"))
    assert store.get_task("t-1").status == "paused"
    assert os.listdir(tmp_path / "tasks") == ["t-1.json"]


def test_delete_task(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_task(_task())
    assert store.delete_task("t-1") is True
    assert store.get_task("t-1") is None
    assert store.delete_task("t-1") is False


def test_list_filters_by_site(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-a", "site-a"))
    store.put_task(_task("t-b", "site-b"))
    store.put_occurrence(_occ("2026-10-24T09:00:00Z", "t-a", "site-a"))
    store.put_occurrence(_occ("2026-10-24T09:00:00Z", "t-b", "site-b"))

    assert {t.task_id for t in store.list_tasks(None)} == {"t-a", "t-b"}
    assert [t.task_id for t in store.list_tasks("site-a")] == ["t-a"]
    assert [o.task_id for o in store.list_occurrences("site-b", None, None)] == ["t-b"]
    assert store.list_tasks("site-z") == []


def test_list_occurrences_filters_by_due_at_range(tmp_path):
    store = TaskStore(str(tmp_path))
    for due in ("2026-10-24T09:00:00Z", "2026-10-25T09:00:00Z", "2026-10-26T09:00:00Z"):
        store.put_occurrence(_occ(due))

    def due_ats(start, end):
        return [o.due_at for o in store.list_occurrences(None, start, end)]

    # Oldest first, so a calendar can render the list as it stands.
    assert due_ats(None, None) == [
        "2026-10-24T09:00:00Z", "2026-10-25T09:00:00Z", "2026-10-26T09:00:00Z"]
    # start is inclusive, end is exclusive: adjacent windows never share a slot.
    assert due_ats("2026-10-25T09:00:00Z", None) == [
        "2026-10-25T09:00:00Z", "2026-10-26T09:00:00Z"]
    assert due_ats(None, "2026-10-25T09:00:00Z") == ["2026-10-24T09:00:00Z"]
    assert due_ats("2026-10-24T12:00:00Z", "2026-10-26T00:00:00Z") == ["2026-10-25T09:00:00Z"]
    # Compared as instants, not strings: the same moment written with an offset.
    assert due_ats("2026-10-25T18:00:00+09:00", "2026-10-26T09:00:00Z") == ["2026-10-25T09:00:00Z"]


def test_rejects_path_traversal_id(tmp_path):
    store = TaskStore(str(tmp_path))
    for call in (
        lambda: store.get_task("../escape"),
        lambda: store.delete_task(r"..\escape"),
        lambda: store.get_occurrence("../escape"),
        lambda: store.get_task("a:b"),  # NTFS alternate data stream
        lambda: store.get_task(".."),
        lambda: store.get_task(""),
    ):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 400


def test_put_rejects_traversal_id_and_writes_nothing(tmp_path):
    store = TaskStore(str(tmp_path / "root"))
    bad = _task("../escape")
    with pytest.raises(HTTPException) as e:
        store.put_task(bad)
    assert e.value.status_code == 400
    bad_occ = _occ("2026-10-24T09:00:00Z").model_copy(update={"occurrence_id": "../escape"})
    with pytest.raises(HTTPException) as e:
        store.put_occurrence(bad_occ)
    assert e.value.status_code == 400
    # Nothing landed in the parent directory nor inside the store.
    # A real "../escape" from root/tasks (or root/occurrences) lands at root/escape.json.
    assert not (tmp_path / "root" / "escape.json").exists()
    assert not (tmp_path / "escape.json").exists()
    assert os.listdir(tmp_path / "root" / "tasks") == []
    assert os.listdir(tmp_path / "root" / "occurrences") == []


def test_tasks_and_occurrences_do_not_see_each_other(tmp_path):
    # Same id in both namespaces must not collide or cross over.
    store = TaskStore(str(tmp_path))
    store.put_task(_task("shared-id"))
    assert store.get_occurrence("shared-id") is None
    assert store.list_occurrences(None, None, None) == []


def test_unreadable_file_is_skipped_not_fatal(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-good"))
    store.put_occurrence(_occ("2026-10-24T09:00:00Z"))
    (tmp_path / "tasks" / "t-bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "tasks" / "t-invalid.json").write_text('{"task_id": "t-invalid"}', encoding="utf-8")
    (tmp_path / "occurrences" / "o-bad.json").write_text("", encoding="utf-8")

    assert [t.task_id for t in store.list_tasks(None)] == ["t-good"]
    assert len(store.list_occurrences(None, None, None)) == 1
    assert store.get_task("t-bad") is None


def test_file_whose_id_disagrees_with_its_name_is_skipped(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-real"))
    # Copied by hand to another name: listing must not report it under two ids.
    (tmp_path / "tasks" / "t-copy.json").write_text(
        (tmp_path / "tasks" / "t-real.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert [t.task_id for t in store.list_tasks(None)] == ["t-real"]


def test_list_occurrences_rejects_an_unparseable_bound(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_occurrence(_occ("2026-10-24T09:00:00Z"))
    with pytest.raises(HTTPException) as e:
        store.list_occurrences(None, "yesterday", None)
    assert e.value.status_code == 400


def test_occurrence_with_unparseable_due_at_is_left_out_of_a_bounded_listing(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_occurrence(_occ("2026-10-24T09:00:00Z"))
    broken = _occ("2026-10-25T09:00:00Z").model_copy(update={"due_at": "not-a-time"})
    store.put_occurrence(broken)
    assert len(store.list_occurrences(None, None, None)) == 2
    assert [o.due_at for o in store.list_occurrences(None, "2026-10-01T00:00:00Z", None)] == [
        "2026-10-24T09:00:00Z"]


def test_list_tasks_is_ordered_by_task_id(tmp_path):
    store = TaskStore(str(tmp_path))
    for task_id in ("t-c", "t-a", "t-b"):
        store.put_task(_task(task_id))
    assert [t.task_id for t in store.list_tasks(None)] == ["t-a", "t-b", "t-c"]


def test_occurrences_due_at_the_same_instant_are_ordered_by_id(tmp_path):
    store = TaskStore(str(tmp_path))
    for task_id in ("t-b", "t-a"):
        store.put_occurrence(_occ("2026-10-24T09:00:00Z", task_id))
    assert [o.task_id for o in store.list_occurrences(None, None, None)] == ["t-a", "t-b"]


def test_unparseable_due_at_sorts_first_in_an_unbounded_listing(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_occurrence(_occ("2026-10-24T09:00:00Z"))
    store.put_occurrence(_occ("2026-10-25T09:00:00Z").model_copy(update={"due_at": "not-a-time"}))
    assert [o.due_at for o in store.list_occurrences(None, None, None)] == [
        "not-a-time", "2026-10-24T09:00:00Z"]


# -- cascade and orphan pruning (2026-10-26) -----------------------------------
#
# The policy under test: an occurrence is only ever deleted while it is a
# promise nobody has acted on ("scheduled", "due", no run). Anything that
# started, finished, or was decided by the scheduler is history and is kept.

def test_delete_occurrence(tmp_path):
    store = TaskStore(str(tmp_path))
    occ = _occ("2026-10-24T09:00:00Z")
    store.put_occurrence(occ)

    assert store.delete_occurrence(occ.occurrence_id) is True
    assert store.get_occurrence(occ.occurrence_id) is None
    assert store.delete_occurrence(occ.occurrence_id) is False  # idempotent
    with pytest.raises(HTTPException) as err:
        store.delete_occurrence("../escape")
    assert err.value.status_code == 400


def test_occurrences_for_task_returns_only_that_tasks(tmp_path):
    store = TaskStore(str(tmp_path))
    mine = _occ("2026-10-24T09:00:00Z", task_id="t-1")
    other = _occ("2026-10-24T09:00:00Z", task_id="t-10")  # a prefix match must not count
    store.put_occurrence(mine)
    store.put_occurrence(other)

    assert store.occurrences_for_task("t-1") == [mine]
    assert store.occurrences_for_task("nope") == []


def test_task_exists_is_about_the_file_not_whether_it_parses(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-1"))
    (tmp_path / "tasks" / "t-2.json").write_text("{not json", encoding="utf-8")

    assert store.task_exists("t-1") is True
    # Unreadable is not absent: get_task() says None for both, and treating a
    # corrupt file as "deleted" would delete its occurrences.
    assert store.get_task("t-2") is None
    assert store.task_exists("t-2") is True
    assert store.task_exists("t-3") is False
    assert store.task_exists("../escape") is False  # an id the store rejects names no task


@pytest.mark.parametrize("status", ["scheduled", "due", "skipped", "cancelled"])
def test_delete_task_cascade_removes_unstarted_occurrences(tmp_path, status):
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-1"))
    occ = _occ("2026-10-24T09:00:00Z", status=status)
    store.put_occurrence(occ)

    result = store.delete_task_cascade("t-1")

    assert (result.task_deleted, result.occurrences_deleted, result.occurrences_kept) == (True, [occ.occurrence_id], [])
    assert store.get_task("t-1") is None
    assert store.get_occurrence(occ.occurrence_id) is None


@pytest.mark.parametrize("status", ["dispatched", "running", "succeeded", "failed"])
def test_delete_task_cascade_keeps_history(tmp_path, status):
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-1"))
    occ = _occ("2026-10-24T09:00:00Z", status=status)
    store.put_occurrence(occ)

    result = store.delete_task_cascade("t-1")

    assert result.task_deleted is True
    assert (result.occurrences_deleted, result.occurrences_kept) == ([], [occ.occurrence_id])
    assert store.get_occurrence(occ.occurrence_id) == occ


def test_a_scheduled_occurrence_that_carries_a_run_is_kept(tmp_path):
    # status says "nothing happened", run_id says otherwise: believe the run.
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-1"))
    occ = _occ("2026-10-24T09:00:00Z", status="scheduled", run_id="run-1")
    store.put_occurrence(occ)

    result = store.delete_task_cascade("t-1")

    assert result.occurrences_kept == [occ.occurrence_id]
    assert store.get_occurrence(occ.occurrence_id) == occ


def test_delete_task_cascade_leaves_other_tasks_alone_and_is_idempotent(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-1"))
    store.put_task(_task("t-2"))
    store.put_occurrence(_occ("2026-10-24T09:00:00Z", task_id="t-1"))
    survivor = _occ("2026-10-24T09:00:00Z", task_id="t-2")
    store.put_occurrence(survivor)

    store.delete_task_cascade("t-1")
    again = store.delete_task_cascade("t-1")

    assert (again.task_deleted, again.occurrences_deleted, again.occurrences_kept) == (False, [], [])
    assert store.get_task("t-2") is not None
    assert store.get_occurrence(survivor.occurrence_id) == survivor


def test_prune_removes_unstarted_occurrences_of_a_missing_task_whatever_their_date(tmp_path):
    store = TaskStore(str(tmp_path))
    gone = [
        _occ("2026-10-25T09:00:00Z", task_id="ghost"),                                          # future
        _occ("2026-10-26T09:00:00Z", task_id="ghost", status="due"),
        _occ("2026-10-24T08:00:00Z", task_id="ghost"),                                          # due exactly now
        _occ("2026-10-20T09:00:00Z", task_id="ghost"),                                          # long past
        _occ("2026-10-21T09:00:00Z", task_id="ghost", status="skipped", skip_reason="no_executor"),
        _occ("2026-10-22T09:00:00Z", task_id="ghost", status="cancelled", message="the task was deleted"),
    ]
    for occ in gone:
        store.put_occurrence(occ)

    pruned = store.prune_orphan_occurrences()

    assert sorted(pruned) == sorted(o.occurrence_id for o in gone)
    assert store.list_occurrences() == []


def test_a_past_dated_orphaned_skipped_occurrence_is_pruned(tmp_path):
    # The bug's slow-motion return: yesterday's "scheduled" becomes "skipped",
    # and must not become permanent debris labelled as a run of a deleted mission.
    store = TaskStore(str(tmp_path))
    occ = _occ("2026-10-23T06:00:00Z", task_id="ghost", status="skipped", skip_reason="no_executor")
    store.put_occurrence(occ)

    assert store.prune_orphan_occurrences() == [occ.occurrence_id]


@pytest.mark.parametrize("status", ["skipped", "cancelled", "scheduled", "due"])
def test_an_orphan_with_a_run_id_survives_whatever_its_status(tmp_path, status):
    store = TaskStore(str(tmp_path))
    occ = _occ("2026-10-23T06:00:00Z", task_id="ghost", status=status, run_id="run-1")
    store.put_occurrence(occ)

    assert store.prune_orphan_occurrences() == []
    assert store.get_occurrence(occ.occurrence_id) == occ


@pytest.mark.parametrize("status", ["dispatched", "running"])
def test_an_orphaned_in_flight_occurrence_survives_even_with_no_run_id(tmp_path, status):
    # `_dispatch` writes "dispatched" before the run exists: no run_id yet does
    # not mean nothing started.
    store = TaskStore(str(tmp_path))
    occ = _occ("2026-10-25T06:00:00Z", task_id="ghost", status=status)
    store.put_occurrence(occ)

    assert store.prune_orphan_occurrences() == []
    assert store.get_occurrence(occ.occurrence_id) == occ


def test_prune_keeps_everything_that_is_not_an_unstarted_orphan(tmp_path):
    store = TaskStore(str(tmp_path))
    store.put_task(_task("t-1"))
    kept = [
        _occ("2026-10-25T09:00:00Z", task_id="t-1"),                          # task exists
        _occ("2026-10-25T09:00:00Z", task_id="ghost", status="dispatched"),   # in flight
        _occ("2026-10-25T10:00:00Z", task_id="ghost", status="running"),
        _occ("2026-10-25T11:00:00Z", task_id="ghost", status="succeeded"),    # history
        _occ("2026-10-25T12:00:00Z", task_id="ghost", status="failed"),
        _occ("2026-10-25T13:00:00Z", task_id="ghost", status="skipped", run_id="run-2"),
        _occ("2026-10-25T14:00:00Z", task_id="ghost", status="cancelled", run_id="run-3"),
        _occ("2026-10-25T15:00:00Z", task_id="ghost", run_id="run-1"),        # carries a run
    ]
    for occ in kept:
        store.put_occurrence(occ)

    assert store.prune_orphan_occurrences() == []
    assert sorted(o.occurrence_id for o in store.list_occurrences()) == sorted(o.occurrence_id for o in kept)


def test_prune_does_not_need_a_parseable_due_at(tmp_path):
    # due_at is irrelevant to an orphan that never started anything.
    store = TaskStore(str(tmp_path))
    odd = _occ("2026-10-25T09:00:00Z", task_id="ghost").model_copy(update={"due_at": "sometime"})
    store.put_occurrence(odd)

    assert store.prune_orphan_occurrences() == [odd.occurrence_id]


def test_prune_does_not_mistake_an_unreadable_task_file_for_a_deleted_task(tmp_path):
    store = TaskStore(str(tmp_path))
    (tmp_path / "tasks" / "t-1.json").write_text("{not json", encoding="utf-8")
    occ = _occ("2026-10-25T09:00:00Z", task_id="t-1")
    store.put_occurrence(occ)

    assert store.prune_orphan_occurrences() == []
    assert store.get_occurrence(occ.occurrence_id) == occ


def test_prune_skips_an_occurrence_whose_task_id_the_store_rejects(tmp_path):
    store = TaskStore(str(tmp_path))
    occ = _occ("2026-10-25T09:00:00Z", task_id="ghost").model_copy(update={"task_id": "../x"})
    store.put_occurrence(occ)

    assert store.prune_orphan_occurrences() == []
    assert store.get_occurrence(occ.occurrence_id) is not None
