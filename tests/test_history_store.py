"""history/store.py on both engines: migrations, incidents and their workflow,
telemetry history with server-side thinning, the audit log, retention.

Every test here runs on SQLite, and on Postgres too when TEST_DATABASE_URL is
set (the `history_db` fixture in conftest.py)."""
import pytest

from history.db import HistoryUnavailable
from history.records import AuditRecord, IncidentRecord, PerceptionRecord, SampleRecord
from history.store import LATEST_MIGRATION, HistoryStore, IncidentConflict, IncidentNotFound

T0 = 1_762_840_800.0  # 2025-11-11T06:00:00Z
DAY = 86_400.0


def incident(iid="rover-a-1", at="2026-11-11T06:00:00+00:00", robot="rover-a", site="site-a", **extra):
    payload = {
        "incident_id": iid, "robot_id": robot, "site_id": site, "rule_id": "builtin.fall_alert",
        "label": "fallen_person", "severity": "critical", "opened_at": at, "camera": "front",
        "detections": [{"label": "person", "confidence": 0.91, "bbox": [0.1, 0.1, 0.2, 0.5],
                        "camera": "front", "model": "yolo"}],
        "evidence_snapshot_ids": [], "evidence_pending": True, "state": "open",
        "message": "Fallen person detected", **extra,
    }
    return IncidentRecord(payload=payload, robot_id=robot, site_id=site, received_at=T0)


def sample(at, robot="rover-a", battery=80.0, **over):
    fields = dict(robot_id=robot, site_id="site-a", at=at, battery_pct=battery, x_m=1.0, y_m=2.0,
                  heading_deg=90.0, position_quality="localized", speed_mps=0.0, abstract_state="STAND",
                  sdk_state="balance_stand", posture="upright", control_owner=None, active_run_id=None,
                  link="live", reason="interval")
    fields.update(over)
    return SampleRecord(**fields)


# -- migrations -------------------------------------------------------------------

def test_migrations_apply_from_empty_and_re_run_harmlessly(history_db):
    store = HistoryStore(history_db)
    assert store.migrate() == LATEST_MIGRATION
    assert store.counts() == {"incidents": 0, "telemetry_samples": 0, "perception_status": 0, "audit_log": 0}
    again = HistoryStore(history_db)
    assert again.migrate() == LATEST_MIGRATION
    assert again.level() == LATEST_MIGRATION
    store.close()
    again.close()


def test_a_database_from_a_newer_backend_is_refused(history_store, history_db):
    history_store.execute_raw("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                              (LATEST_MIGRATION + 7, "from the future", T0))
    newer = HistoryStore(history_db)
    with pytest.raises(HistoryUnavailable) as refused:
        newer.migrate()
    assert f"migration {LATEST_MIGRATION + 7}" in refused.value.message
    assert f"knows {LATEST_MIGRATION}" in refused.value.message
    newer.close()


# -- incidents ----------------------------------------------------------------------

def test_an_incident_is_stored_as_published(history_store):
    history_store.write([incident(position={"x_m": 3.5, "y_m": -1.0, "map_id": "map-office"}, run_id="run-9")])

    got = history_store.get_incident("rover-a-1")
    assert (got["robot_id"], got["site_id"], got["label"], got["severity"]) == (
        "rover-a", "site-a", "fallen_person", "critical")
    assert got["opened_at"] == "2026-11-11T06:00:00Z"
    assert got["confidence"] == pytest.approx(0.91)
    assert (got["x_m"], got["y_m"], got["map_id"], got["run_id"]) == (3.5, -1.0, "map-office", "run-9")
    assert got["state"] == "open" and got["acknowledged_by"] is None
    assert got["payload"]["message"] == "Fallen person detected"


def test_a_redelivered_incident_only_moves_last_seen(history_store):
    history_store.write([incident()])
    first = history_store.get_incident("rover-a-1")
    again = incident(message="something else", severity="info")
    again = IncidentRecord(payload=again.payload, robot_id="rover-a", site_id="site-a", received_at=T0 + 30)
    history_store.write([again])

    got = history_store.get_incident("rover-a-1")
    assert got["last_seen"] == "2025-11-11T06:00:30Z"
    assert {k: v for k, v in got.items() if k != "last_seen"} == {
        k: v for k, v in first.items() if k != "last_seen"}


def test_a_redelivery_never_reopens_a_resolved_incident(history_store):
    history_store.write([incident()])
    history_store.resolve("rover-a-1", "sue", "confirmed", None, T0 + 10)
    history_store.write([incident()])
    assert history_store.get_incident("rover-a-1")["state"] == "resolved"


def test_incidents_are_listed_newest_first_with_filters_and_a_cursor(history_store):
    history_store.write([
        incident("i-1", "2026-11-11T06:00:00Z"),
        incident("i-2", "2026-11-11T06:01:00Z", robot="rover-b"),
        incident("i-3", "2026-11-11T06:02:00Z", label="person_lying"),
        incident("i-4", "2026-11-11T06:03:00Z", site="site-b"),
    ])
    items, cursor = history_store.list_incidents(limit=2)
    assert [i["incident_id"] for i in items] == ["i-4", "i-3"]
    rest, end = history_store.list_incidents(limit=2, cursor=cursor)
    assert [i["incident_id"] for i in rest] == ["i-2", "i-1"] and end is None

    assert [i["incident_id"] for i in history_store.list_incidents(site_id="site-a")[0]] == ["i-3", "i-2", "i-1"]
    assert [i["incident_id"] for i in history_store.list_incidents(robot_id="rover-b")[0]] == ["i-2"]
    assert [i["incident_id"] for i in history_store.list_incidents(kind="person_lying")[0]] == ["i-3"]
    window = history_store.list_incidents(since="2026-11-11T06:01:00Z", until="2026-11-11T06:03:00Z")[0]
    assert [i["incident_id"] for i in window] == ["i-3", "i-2"]
    history_store.acknowledge("i-1", "ana", T0)
    assert [i["incident_id"] for i in history_store.list_incidents(state="acknowledged")[0]] == ["i-1"]


# -- the workflow ---------------------------------------------------------------------

def test_open_acknowledged_resolved_records_who_and_when(history_store):
    history_store.write([incident()])

    acked = history_store.acknowledge("rover-a-1", "ana", T0 + 5)
    assert (acked["state"], acked["acknowledged_by"], acked["acknowledged_at"]) == (
        "acknowledged", "ana", "2025-11-11T06:00:05Z")
    done = history_store.resolve("rover-a-1", "sue", "false_alarm", "a mannequin", T0 + 9)
    assert (done["state"], done["resolved_by"], done["outcome"], done["note"]) == (
        "resolved", "sue", "false_alarm", "a mannequin")
    assert done["acknowledged_by"] == "ana"


def test_resolving_an_open_incident_records_both(history_store):
    history_store.write([incident()])
    done = history_store.resolve("rover-a-1", "sue", "confirmed", None, T0 + 9)
    assert (done["acknowledged_by"], done["resolved_by"], done["acknowledged_at"]) == (
        "sue", "sue", "2025-11-11T06:00:09Z")


def test_repeating_an_action_changes_nothing(history_store):
    history_store.write([incident()])
    first = history_store.acknowledge("rover-a-1", "ana", T0 + 5)
    assert history_store.acknowledge("rover-a-1", "bob", T0 + 50) == first
    resolved = history_store.resolve("rover-a-1", "sue", "confirmed", "ok", T0 + 60)
    assert history_store.resolve("rover-a-1", "bob", "confirmed", "ok", T0 + 70) == resolved
    # Acknowledging a resolved one is not a change of mind anyone can make...
    with pytest.raises(IncidentConflict) as refused:
        history_store.acknowledge("rover-a-1", "bob", T0 + 80)
    assert refused.value.message == "incident rover-a-1 is already resolved; it cannot be acknowledged again"
    # ...and nor is resolving it differently.
    with pytest.raises(IncidentConflict) as refused:
        history_store.resolve("rover-a-1", "bob", "false_alarm", None, T0 + 90)
    assert refused.value.message == (
        "incident rover-a-1 was already resolved as confirmed by sue; it cannot be resolved again")


def test_a_workflow_action_on_an_unknown_incident_is_not_found(history_store):
    with pytest.raises(IncidentNotFound):
        history_store.acknowledge("nope", "ana", T0)
    with pytest.raises(IncidentNotFound):
        history_store.resolve("nope", "ana", "confirmed", None, T0)


def test_an_unknown_outcome_is_refused(history_store):
    history_store.write([incident()])
    with pytest.raises(ValueError):
        history_store.resolve("rover-a-1", "ana", "maybe", None, T0)


def test_each_workflow_change_writes_an_audit_row_with_its_actor(history_store):
    history_store.write([incident()])
    history_store.acknowledge("rover-a-1", "ana", T0 + 5)
    history_store.acknowledge("rover-a-1", "ana", T0 + 6)           # idempotent: no second row
    history_store.resolve("rover-a-1", "sue", "confirmed", "seen on camera", T0 + 9)

    rows, _ = history_store.list_audit()
    assert [(r["actor"], r["action"], r["target"], r["outcome"]) for r in rows] == [
        ("sue", "incident.resolve", "rover-a-1", "confirmed"),
        ("ana", "incident.acknowledge", "rover-a-1", "ok"),
    ]
    assert rows[0]["detail"]["note"] == "seen on camera"


# -- telemetry history -------------------------------------------------------------------

def test_history_returns_samples_in_order_and_unknown_battery_stays_unknown(history_store):
    history_store.write([sample(T0 + 10, battery=None), sample(T0, battery=55.0)])
    rows = history_store.history("rover-a", T0 - 1, T0 + 100, 500)
    assert [r["battery_pct"] for r in rows] == [55.0, None]
    assert rows[0]["at"] == "2025-11-11T06:00:00Z"
    assert rows[0]["position_quality"] == "localized"


def test_history_is_thinned_on_the_server(history_store):
    history_store.write([sample(T0 + i) for i in range(1000)])
    rows = history_store.history("rover-a", T0, T0 + 1000, 100)
    assert 50 <= len(rows) <= 100
    ats = [r["at"] for r in rows]
    assert ats == sorted(ats)
    # Spread over the whole window, not the first 100 seconds.
    assert rows[-1]["at"] >= "2025-11-11T06:15:00Z"
    assert len(history_store.history("rover-a", T0, T0 + 1000, 5000)) == 1000


def test_history_is_per_robot_and_reports_whether_any_exists(history_store):
    history_store.write([sample(T0), sample(T0, robot="rover-b")])
    assert len(history_store.history("rover-a", T0 - 1, T0 + 1, 500)) == 1
    assert history_store.has_samples("rover-b") and not history_store.has_samples("rover-z")


def test_history_can_be_limited_to_the_callers_sites(history_store):
    # A robot that moved from site-a to site-b: a site-b reader sees only site-b's samples.
    history_store.write([sample(T0 + i, site_id="site-a") for i in range(5)]
                        + [sample(T0 + 10 + i, site_id="site-b") for i in range(5)]
                        + [sample(T0 + 20, site_id=None)])
    rows = lambda sites, n=500: history_store.history("rover-a", T0 - 1, T0 + 100, n, sites=sites)  # noqa: E731
    assert len(rows(None)) == 11
    assert len(rows(frozenset({"site-b"}))) == 6          # plus the sample with no site
    assert len(rows(frozenset({"site-a", "site-b"}))) == 11
    assert rows(frozenset()) == []
    assert 1 <= len(rows(frozenset({"site-b"}), n=2)) <= 2  # the thinned query filters too
    assert history_store.has_samples("rover-a", frozenset({"site-b"}))
    history_store.write([sample(T0, robot="rover-c", site_id="site-a")])
    assert not history_store.has_samples("rover-c", frozenset({"site-z"}))
    assert not history_store.has_samples("rover-a", frozenset())


def test_a_robots_site_is_its_newest_sample_that_names_one(history_store):
    assert history_store.site_of_robot("rover-a") is None
    history_store.write([sample(T0, site_id="site-a"), sample(T0 + 10, site_id="site-b"),
                         sample(T0 + 20, site_id=None)])
    assert history_store.site_of_robot("rover-a") == "site-b"
    assert history_store.site_of_robot("rover-z") is None


# -- audit ------------------------------------------------------------------------------------

def test_the_audit_log_is_filtered_and_paged_newest_first(history_store):
    history_store.write([
        AuditRecord(at=T0 + i, actor="ana" if i % 2 else "sue", action="command.publish",
                    target="rover-a", outcome="sent", detail={"i": i}) for i in range(5)
    ])
    rows, cursor = history_store.list_audit(limit=2)
    assert [r["detail"]["i"] for r in rows] == [4, 3]
    rows, _ = history_store.list_audit(limit=10, cursor=cursor)
    assert [r["detail"]["i"] for r in rows] == [2, 1, 0]
    assert [r["detail"]["i"] for r in history_store.list_audit(actor="ana")[0]] == [3, 1]
    assert [r["detail"]["i"] for r in history_store.list_audit(since="2025-11-11T06:00:02Z")[0]] == [4, 3, 2]


# -- retention --------------------------------------------------------------------------------

def test_retention_prunes_telemetry_and_old_perception_but_keeps_incidents_and_audit(history_store):
    old, new = T0 - 40 * DAY, T0 - DAY
    history_store.write([
        sample(old), sample(new),
        PerceptionRecord(robot_id="rover-a", site_id="site-a", at=old, source_state="live", fps=10.0,
                         camera="front", detectors=[], payload={}),
        PerceptionRecord(robot_id="rover-a", site_id="site-a", at=old + 1, source_state="unavailable",
                         fps=None, camera="front", detectors=[], payload={}),
        PerceptionRecord(robot_id="rover-b", site_id="site-a", at=old, source_state="live", fps=10.0,
                         camera="front", detectors=[], payload={}),
        incident(at="2025-10-01T00:00:00Z"),
        AuditRecord(at=old, actor="ana", action="command.publish", target="rover-a", outcome="sent", detail={}),
    ])

    pruned = history_store.prune(T0, retention_days=30)

    assert pruned == {"telemetry_samples": 1, "perception_status": 1}
    counts = history_store.counts()
    assert counts == {"incidents": 1, "telemetry_samples": 1, "perception_status": 2, "audit_log": 1}
    # The newest status of each robot is kept however old: it is still its latest.
    latest = history_store.latest_perception()
    assert {(p["robot_id"], p["source_state"]) for p in latest} == {
        ("rover-a", "unavailable"), ("rover-b", "live")}
