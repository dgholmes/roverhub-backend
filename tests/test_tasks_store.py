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
