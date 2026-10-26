"""Tasks and occurrences API: tasks/router.py."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared.schemas.tasks import TaskOccurrence
from tasks.router import MAX_TASK_BYTES, build_tasks_router
from tasks.store import TaskStore


class _Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"2026-10-23T00:00:{self.n:02d}Z"


def _client(tmp_path, clock=None, **router_kwargs):
    app = FastAPI()
    app.include_router(build_tasks_router(str(tmp_path), clock=clock or _Clock(), **router_kwargs))
    return TestClient(app)


def _schedule(**extra):
    return {
        "schedule_id": "s-1", "recurrence": "daily", "start_at": "2026-10-24T09:00",
        "timezone": "Asia/Seoul", **extra,
    }


def _task(task_id="t-1", site_id="site-a", mission_id="m-1", **extra):
    return {
        "task_id": task_id, "mission_id": mission_id, "site_id": site_id,
        "robot_id": "robot-1", "created_by": "operator-dev",
        "created_at": "2026-10-22T00:00:00Z", "updated_at": "1999-01-01T00:00:00Z",
        **extra,
    }


def _seed(tmp_path, *due_ats, site_id="site-a"):
    store = TaskStore(str(tmp_path))
    for due_at in due_ats:
        compact = due_at.replace("-", "").replace(":", "")
        store.put_occurrence(TaskOccurrence(
            occurrence_id=f"t-1-{compact}", task_id="t-1", mission_id="m-1",
            site_id=site_id, robot_id="robot-1", due_at=due_at,
        ))


def _due_ats(response):
    assert response.status_code == 200, response.text
    return [o["due_at"] for o in response.json()["items"]]


# -- tasks CRUD --------------------------------------------------------------

def test_put_then_get(tmp_path):
    client = _client(tmp_path)
    put = client.put("/tasks/t-1", json=_task(schedule=_schedule()))
    assert put.status_code == 200
    assert put.json()["schedule"]["timezone"] == "Asia/Seoul"
    assert (tmp_path / "tasks" / "t-1.json").is_file()
    assert client.get("/tasks/t-1").json() == put.json()


def test_server_stamps_updated_at(tmp_path):
    client = _client(tmp_path)
    body = client.put("/tasks/t-1", json=_task()).json()
    assert body["updated_at"] == "2026-10-23T00:00:01Z"  # the clock's, not the body's 1999


def test_a_create_is_stored_at_version_1_and_each_later_write_increments(tmp_path):
    client = _client(tmp_path)
    created = client.put("/tasks/t-1", json=_task()).json()  # the body's default version is 1
    assert created["version"] == 1                           # a first version, not 2
    second = client.put("/tasks/t-1", json={**_task(), "version": created["version"]}).json()
    assert second["version"] == 2
    third = client.put("/tasks/t-1", json={**_task(), "version": second["version"]}).json()
    assert third["version"] == 3
    assert client.get("/tasks/t-1").json()["version"] == 3
    # And it is what is on disk, for anyone opening the file.
    assert TaskStore(str(tmp_path)).get_task("t-1").version == 3


def test_put_with_stale_version_returns_409_and_the_stored_entity(tmp_path):
    client = _client(tmp_path)
    created = client.put("/tasks/t-1", json=_task()).json()                      # stored at 1
    winner = client.put("/tasks/t-1", json={**_task(status="paused"), "version": created["version"]})
    assert winner.status_code == 200 and winner.json()["version"] == 2
    # A second writer who also read version 1 now holds a stale copy.
    loser = client.put("/tasks/t-1", json={**_task(status="archived"), "version": created["version"]})
    assert loser.status_code == 409
    assert loser.json() == winner.json()  # the stored task, so the console can show what changed
    stored = client.get("/tasks/t-1").json()
    assert stored["status"] == "paused" and stored["version"] == 2  # the loser wrote nothing


def test_a_create_is_stored_at_version_1_whatever_version_the_body_carries(tmp_path):
    client = _client(tmp_path)
    created = client.put("/tasks/t-1", json={**_task(), "version": 99}).json()
    assert created["version"] == 1
    assert TaskStore(str(tmp_path)).get_task("t-1").version == 1


def test_put_with_a_version_ahead_of_the_stored_one_is_also_409(tmp_path):
    client = _client(tmp_path)
    client.put("/tasks/t-1", json=_task())
    assert client.put("/tasks/t-1", json={**_task(), "version": 99}).status_code == 409


def test_body_task_id_must_match_the_path(tmp_path):
    client = _client(tmp_path)
    assert client.put("/tasks/t-1", json=_task("t-2")).status_code == 400
    assert client.get("/tasks").json() == {"items": []}


def test_oversized_body_is_rejected(tmp_path):
    client = _client(tmp_path)
    huge = _task(schedule=_schedule(skip_dates=["2026-10-24"] * (MAX_TASK_BYTES // 8)))
    assert client.put("/tasks/t-1", json=huge).status_code == 413
    assert client.get("/tasks/t-1").status_code == 404


def test_a_body_with_no_content_length_is_capped_while_it_streams(tmp_path):
    # Chunked transfer declares no length, so only the running count can stop it.
    def chunks():
        for _ in range(MAX_TASK_BYTES // 1024 + 2):
            yield b" " * 1024

    response = _client(tmp_path).put(
        "/tasks/t-1", content=chunks(), headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_invalid_id_is_400_before_the_body_is_read(tmp_path):
    client = _client(tmp_path)
    # An unparseable body would be a 422 if the body were read first.
    bad = client.put("/tasks/a%20b", content=b"not json", headers={"content-type": "application/json"})
    assert bad.status_code == 400


def test_invalid_body_is_422(tmp_path):
    client = _client(tmp_path)
    body = _task()
    del body["mission_id"]
    assert client.put("/tasks/t-1", json=body).status_code == 422
    assert client.put("/tasks/t-1", content=b"not json",
                      headers={"content-type": "application/json"}).status_code == 422


def test_the_listing_path_is_not_a_task_id(tmp_path):
    client = _client(tmp_path)
    # GET /tasks/occurrences is the listing; a task stored under that id could never be read back.
    assert client.put("/tasks/occurrences", json=_task("occurrences")).status_code == 400


def test_list_filters_and_orders(tmp_path):
    client = _client(tmp_path)
    client.put("/tasks/b", json=_task("b", site_id="site-a", mission_id="m-1"))
    client.put("/tasks/a", json=_task("a", site_id="site-a", mission_id="m-2"))
    client.put("/tasks/c", json=_task("c", site_id="site-b", mission_id="m-1"))

    def ids(**params):
        return [t["task_id"] for t in client.get("/tasks", params=params).json()["items"]]

    assert ids() == ["a", "b", "c"]
    assert ids(site_id="site-a") == ["a", "b"]
    assert ids(mission_id="m-1") == ["b", "c"]
    assert ids(site_id="site-a", mission_id="m-1") == ["b"]
    assert ids(site_id="nowhere") == []


def test_unknown_task_is_404(tmp_path):
    client = _client(tmp_path)
    assert client.get("/tasks/nope").status_code == 404
    assert client.delete("/tasks/nope").status_code == 404
    assert client.post("/tasks/nope/run-now").status_code == 404


def test_delete_then_404(tmp_path):
    client = _client(tmp_path)
    client.put("/tasks/t-1", json=_task())
    assert client.delete("/tasks/t-1").status_code == 204
    assert client.get("/tasks/t-1").status_code == 404


def _occ(task_id, due_at, **extra):
    compact = due_at.replace("-", "").replace(":", "")
    return TaskOccurrence(
        occurrence_id=f"{task_id}-{compact}", task_id=task_id, mission_id="m-1",
        site_id="site-a", robot_id="robot-1", due_at=due_at, **extra,
    )


def test_delete_removes_the_tasks_unstarted_occurrences(tmp_path, caplog):
    client = _client(tmp_path)
    client.put("/tasks/t-1", json=_task())
    store = TaskStore(str(tmp_path))
    store.put_occurrence(_occ("t-1", "2026-10-25T09:00:00Z"))
    store.put_occurrence(_occ("t-1", "2026-10-26T09:00:00Z", status="due"))

    with caplog.at_level("WARNING", logger="roverhub.tasks"):
        assert client.delete("/tasks/t-1").status_code == 204

    assert store.list_occurrences() == []
    # Not silent: what went is in the log, with its ids.
    assert "t-1" in caplog.text and "t-1-20261025T090000Z" in caplog.text


def test_delete_keeps_history_and_other_tasks_occurrences(tmp_path, caplog):
    client = _client(tmp_path)
    client.put("/tasks/t-1", json=_task())
    store = TaskStore(str(tmp_path))
    ran = _occ("t-1", "2026-10-22T09:00:00Z", status="succeeded", run_id="t-1-20261022T090000Z")
    flying = _occ("t-1", "2026-10-23T09:00:00Z", status="running")
    skipped = _occ("t-1", "2026-10-24T09:00:00Z", status="skipped", skip_reason="no_executor", run_id="r-3")
    other = _occ("t-2", "2026-10-25T09:00:00Z")
    for occ in (ran, flying, skipped, other):
        store.put_occurrence(occ)

    with caplog.at_level("WARNING", logger="roverhub.tasks"):
        assert client.delete("/tasks/t-1").status_code == 204

    assert {o.occurrence_id for o in store.list_occurrences()} == {
        ran.occurrence_id, flying.occurrence_id, skipped.occurrence_id, other.occurrence_id,
    }
    assert "kept 3" in caplog.text  # the log says history was left, not just what went


def test_delete_of_an_already_deleted_task_still_clears_its_leftovers(tmp_path):
    # The state the owner's data is in: no task, occurrences left behind.
    client = _client(tmp_path)
    store = TaskStore(str(tmp_path))
    store.put_occurrence(_occ("ghost", "2026-10-25T09:00:00Z"))

    assert client.delete("/tasks/ghost").status_code == 404  # contract unchanged: it was not there
    assert store.list_occurrences() == []
    assert client.delete("/tasks/ghost").status_code == 404  # and idempotent


# -- validation the schema cannot do ------------------------------------------

def test_a_real_timezone_and_start_at_are_accepted(tmp_path):
    client = _client(tmp_path)
    # A distinct id per zone: each is a create, so each must be a plain 200 and
    # not a 409 against the one before.
    for n, tz in enumerate(("Asia/Seoul", "UTC", "America/New_York")):
        response = client.put(f"/tasks/t-{n}", json=_task(f"t-{n}", schedule=_schedule(timezone=tz)))
        assert response.status_code == 200, (tz, response.text)


@pytest.mark.parametrize("zone", [
    "Asia/Seol",        # a typo: ZoneInfoNotFoundError
    "America",          # a directory in the tz database: IsADirectoryError, an OSError
    "",                 # empty
    "../etc/passwd",    # not a key
    "+09:00",           # an offset, which is exactly what the schedule must not store
])
def test_an_unknown_timezone_is_422_and_nothing_is_stored(tmp_path, zone):
    client = _client(tmp_path)
    response = client.put("/tasks/t-1", json=_task(schedule=_schedule(timezone=zone)))
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "schedule", "timezone"]
    assert client.get("/tasks/t-1").status_code == 404


@pytest.mark.parametrize("start_at", [
    "2026-10-24 09:00",            # space, not T
    "2026-10-24T09:00:00",         # seconds
    "2026-10-24T09:00+09:00",      # an offset: the schedule's zone is `timezone`, not this
    "2026-10-24T09:00Z",
    "2026-13-45T09:00",            # right shape, not a date
    "2026-10-24T25:00",            # right shape, not a time
    "tomorrow",
    "",
])
def test_a_malformed_start_at_is_422_and_nothing_is_stored(tmp_path, start_at):
    client = _client(tmp_path)
    response = client.put("/tasks/t-1", json=_task(schedule=_schedule(start_at=start_at)))
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "schedule", "start_at"]
    assert client.get("/tasks/t-1").status_code == 404


def test_a_task_without_a_schedule_needs_neither(tmp_path):
    # A hand-run-only task has no schedule; there is nothing to validate.
    assert _client(tmp_path).put("/tasks/t-1", json=_task()).status_code == 200


def test_both_schedule_errors_are_reported_together(tmp_path):
    response = _client(tmp_path).put(
        "/tasks/t-1", json=_task(schedule=_schedule(timezone="Asia/Seol", start_at="tomorrow")),
    )
    assert response.status_code == 422
    assert {tuple(d["loc"]) for d in response.json()["detail"]} == {
        ("body", "schedule", "timezone"), ("body", "schedule", "start_at"),
    }


# -- occurrences ---------------------------------------------------------------

def test_occurrences_are_filtered_by_range(tmp_path):
    _seed(tmp_path, "2026-10-23T08:00:00Z", "2026-10-24T09:00:00Z", "2026-10-25T09:00:00Z")
    client = _client(tmp_path)
    got = client.get("/tasks/occurrences", params={
        "from": "2026-10-24T00:00:00Z", "to": "2026-10-25T09:00:00Z",
    })
    assert _due_ats(got) == ["2026-10-24T09:00:00Z"]  # `to` is exclusive


def test_occurrences_are_filtered_by_site(tmp_path):
    _seed(tmp_path, "2026-10-24T09:00:00Z")
    _seed(tmp_path, "2026-10-24T10:00:00Z", site_id="site-b")
    client = _client(tmp_path)
    window = {"from": "2026-10-24T00:00:00Z", "to": "2026-10-25T00:00:00Z"}
    assert _due_ats(client.get("/tasks/occurrences", params=window)) == [
        "2026-10-24T09:00:00Z", "2026-10-24T10:00:00Z",
    ]
    assert _due_ats(client.get("/tasks/occurrences", params={**window, "site_id": "site-b"})) == [
        "2026-10-24T10:00:00Z",
    ]


def test_the_window_is_required(tmp_path):
    client = _client(tmp_path)
    assert client.get("/tasks/occurrences").status_code == 422
    assert client.get("/tasks/occurrences", params={"from": "2026-10-24T00:00:00Z"}).status_code == 422


def test_an_unparseable_bound_is_a_400_that_names_the_bound(tmp_path):
    response = _client(tmp_path).get(
        "/tasks/occurrences", params={"from": "2026-10-24T00:00:00Z", "to": "next week"},
    )
    assert response.status_code == 400
    assert "to" in response.json()["detail"] and "next week" in response.json()["detail"]


# 2026-10-24T09:00:00+09:00 is 2026-10-24T00:00:00Z.
_NINE_OCLOCK_SEOUL = "2026-10-24T09:00:00+09:00"


def test_a_literal_plus_offset_in_the_query_string_is_read_as_an_offset(tmp_path):
    # An unescaped '+' in a query string decodes to a space. A client that writes
    # `from=...09:00:00+09:00` by hand sends exactly that, and it is valid.
    _seed(tmp_path, "2026-10-23T23:59:59Z", "2026-10-24T00:00:00Z", "2026-10-24T00:00:01Z")
    url = (
        f"/tasks/occurrences?from={_NINE_OCLOCK_SEOUL}"   # 00:00:00Z, inclusive
        "&to=2026-10-24T09:00:01+09:00"                    # 00:00:01Z, exclusive
    )
    got = _client(tmp_path).get(url)
    assert _due_ats(got) == ["2026-10-24T00:00:00Z"]


def test_a_percent_encoded_plus_offset_is_read_as_an_offset(tmp_path):
    _seed(tmp_path, "2026-10-23T23:59:59Z", "2026-10-24T00:00:00Z")
    got = _client(tmp_path).get(
        "/tasks/occurrences",
        params={"from": _NINE_OCLOCK_SEOUL, "to": "2026-10-25T00:00:00Z"},  # httpx encodes '+' as %2B
    )
    assert _due_ats(got) == ["2026-10-24T00:00:00Z"]


def test_a_negative_offset_and_a_compact_offset_work_too(tmp_path):
    _seed(tmp_path, "2026-10-24T04:59:59Z", "2026-10-24T05:00:00Z")
    client = _client(tmp_path)
    # 2026-10-24T00:00:00-05:00 is 05:00Z.
    assert _due_ats(client.get("/tasks/occurrences", params={
        "from": "2026-10-24T00:00:00-05:00", "to": "2026-10-25T00:00:00Z",
    })) == ["2026-10-24T05:00:00Z"]
    # '+0900' without a colon, unescaped.
    assert _due_ats(client.get(
        "/tasks/occurrences?from=2026-10-24T14:00:00+0900&to=2026-10-25T00:00:00Z",
    )) == ["2026-10-24T05:00:00Z"]


def test_a_space_separator_is_not_mistaken_for_a_plus(tmp_path):
    # "2026-10-24 06:00" has a space where a '+' might have been, but no offset
    # follows a time, so it is a bare timestamp (UTC), not "...T06:00+<junk>".
    _seed(tmp_path, "2026-10-24T05:00:00Z", "2026-10-24T07:00:00Z")
    got = _client(tmp_path).get(
        "/tasks/occurrences", params={"from": "2026-10-24 06:00", "to": "2026-10-25T00:00:00Z"},
    )
    assert _due_ats(got) == ["2026-10-24T07:00:00Z"]


# -- run-now --------------------------------------------------------------------

def test_run_now_queues_an_occurrence_and_does_not_claim_a_run_started(tmp_path):
    client = _client(tmp_path)
    client.put("/tasks/t-1", json=_task())  # a hand-run-only task: no schedule
    response = client.post("/tasks/t-1/run-now")
    assert response.status_code == 202
    occ = response.json()
    assert occ["status"] == "due"            # queued, not "dispatched" or "running"
    assert occ["run_id"] is None             # no run exists
    assert occ["task_id"] == "t-1" and occ["robot_id"] == "robot-1" and occ["mission_id"] == "m-1"
    assert "not started" in occ["message"].lower()
    assert occ["occurrence_id"] == f"t-1-{occ['due_at'].replace('-', '').replace(':', '')}"
    # It is a real record the calendar will list.
    listed = client.get("/tasks/occurrences", params={
        "from": "2026-10-23T00:00:00Z", "to": "2026-10-24T00:00:00Z",
    }).json()["items"]
    assert [o["occurrence_id"] for o in listed] == [occ["occurrence_id"]]


def test_run_now_is_due_at_the_clock_not_in_the_future(tmp_path):
    client = _client(tmp_path)
    client.put("/tasks/t-1", json=_task())  # consumes clock tick 1
    occ = client.post("/tasks/t-1/run-now").json()  # tick 2
    assert occ["due_at"] == "2026-10-23T00:00:02Z"


def test_run_now_twice_in_one_second_is_one_occurrence(tmp_path):
    class _Frozen:
        def __call__(self):
            return "2026-10-23T00:00:05.250000Z"  # sub-second, as the real clock emits

    client = _client(tmp_path, clock=_Frozen())
    client.put("/tasks/t-1", json=_task())
    first = client.post("/tasks/t-1/run-now")
    second = client.post("/tasks/t-1/run-now")
    assert first.json() == second.json()
    assert first.json()["due_at"] == "2026-10-23T00:00:05Z"
    assert len(TaskStore(str(tmp_path)).list_occurrences()) == 1


def test_run_now_says_so_when_the_scheduler_is_off(tmp_path):
    client = _client(tmp_path, scheduler_enabled=lambda: False)
    client.put("/tasks/t-1", json=_task())
    occ = client.post("/tasks/t-1/run-now").json()
    assert occ["status"] == "due"
    message = occ["message"].lower()
    assert "not started" in message
    # Not "decides on its next tick": no tick is coming.
    assert "next tick" not in message and "disabled" in message


def test_run_now_promises_a_tick_only_when_the_scheduler_is_on(tmp_path):
    client = _client(tmp_path, scheduler_enabled=lambda: True)
    client.put("/tasks/t-1", json=_task())
    message = client.post("/tasks/t-1/run-now").json()["message"].lower()
    assert "next tick" in message and "disabled" not in message


@pytest.mark.parametrize("status", ["paused", "archived"])
def test_run_now_refuses_a_task_that_is_not_enabled(tmp_path, status):
    client = _client(tmp_path)
    client.put("/tasks/t-1", json=_task(status=status))
    response = client.post("/tasks/t-1/run-now")
    assert response.status_code == 409
    assert status in response.json()["detail"]
    assert TaskStore(str(tmp_path)).list_occurrences() == []
