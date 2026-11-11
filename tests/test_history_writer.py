"""history/writer.py: the one thread that writes history. MQTT handlers only
hand it records; it batches them, drops the least valuable first when it cannot
keep up, and survives the database going away."""
import threading

import pytest

from history.records import AuditRecord, IncidentRecord, SampleRecord
from history.writer import HistoryWriter

T0 = 1_762_840_800.0


def sample(i, robot="rover-a"):
    return SampleRecord(robot_id=robot, site_id="site-a", at=T0 + i, battery_pct=50.0, x_m=None, y_m=None,
                        heading_deg=None, position_quality=None, speed_mps=0.0, abstract_state="STAND",
                        sdk_state="balance_stand", posture="upright", control_owner=None, active_run_id=None,
                        link="live", reason="interval")


def incident(i):
    payload = {"incident_id": f"i-{i}", "robot_id": "rover-a", "site_id": "site-a", "rule_id": "r",
               "label": "fallen_person", "severity": "critical", "opened_at": "2026-11-11T06:00:00Z",
               "camera": "front", "detections": [], "message": "m"}
    return IncidentRecord(payload=payload, robot_id="rover-a", site_id="site-a", received_at=T0)


def audit(i):
    return AuditRecord(at=T0 + i, actor="ana", action="command.publish", target="rover-a", outcome="sent",
                       detail={"i": i})


class FakeStore:
    def __init__(self, failures=0):
        self.batches = []
        self.failures = failures
        self.pruned = []
        self.lock = threading.Lock()

    def write(self, records):
        with self.lock:
            if self.failures:
                self.failures -= 1
                raise OSError("database is gone")
            self.batches.append(list(records))

    def prune(self, now, retention_days):
        self.pruned.append((now, retention_days))
        return {}

    @property
    def written(self):
        return [r for batch in self.batches for r in batch]


def test_records_are_written_in_batches(tmp_path):
    store = FakeStore()
    writer = HistoryWriter(store, max_queue=1000, batch_rows=200)
    for i in range(450):
        writer.submit(sample(i))
    assert writer.flush(timeout_s=2)
    assert [len(b) for b in store.batches] == [200, 200, 50]
    assert writer.stats()["written"] == 450 and writer.stats()["queue_depth"] == 0


def test_a_full_queue_drops_the_oldest_telemetry_first_and_keeps_incidents():
    store = FakeStore()
    writer = HistoryWriter(store, max_queue=5)
    for i in range(5):
        writer.submit(sample(i))
    for i in range(3):
        writer.submit(incident(i))
    writer.submit(audit(0))

    stats = writer.stats()
    assert stats["dropped"]["telemetry"] == 4
    assert stats["queue_depth"] == 5
    writer.flush(timeout_s=2)
    kinds = [type(r).__name__ for r in store.written]
    assert kinds.count("IncidentRecord") == 3 and kinds.count("AuditRecord") == 1
    # The telemetry that survived is the newest.
    assert [r.at for r in store.written if isinstance(r, SampleRecord)] == [T0 + 4]


def test_incidents_go_over_the_soft_limit_before_any_is_dropped():
    store = FakeStore()
    writer = HistoryWriter(store, max_queue=3)
    for i in range(6):                      # the hard limit is twice the queue
        writer.submit(incident(i))
    assert writer.stats()["dropped"].get("incident", 0) == 0
    writer.submit(incident(6))
    assert writer.stats()["dropped"]["incident"] == 1
    writer.flush(timeout_s=2)
    assert [r.payload["incident_id"] for r in store.written] == [f"i-{i}" for i in range(1, 7)]


def test_telemetry_is_refused_when_the_queue_holds_only_vital_rows():
    writer = HistoryWriter(FakeStore(), max_queue=2)
    writer.submit(incident(0))
    writer.submit(incident(1))
    writer.submit(sample(0))
    assert writer.stats()["dropped"]["telemetry"] == 1


def test_the_database_going_away_keeps_the_batch_and_it_is_written_when_it_returns():
    store = FakeStore(failures=2)
    writer = HistoryWriter(store, max_queue=100, backoff_s=(0.0, 0.0))
    writer.submit(incident(0))
    writer.submit(sample(0))
    assert writer.drain_once() is False
    assert writer.drain_once() is False
    assert writer.stats()["write_errors"] == 2 and writer.stats()["connected"] is False
    assert writer.drain_once() is True
    assert len(store.written) == 2
    assert writer.stats()["connected"] is True and writer.stats()["last_write_at"] is not None


def test_while_the_database_is_away_new_telemetry_still_displaces_old():
    store = FakeStore(failures=1000)
    writer = HistoryWriter(store, max_queue=3, backoff_s=(0.0, 0.0))
    for i in range(3):
        writer.submit(sample(i))
    writer.drain_once()                     # fails: the batch is held, not lost
    for i in range(3, 10):
        writer.submit(sample(i))
    writer.submit(incident(0))
    assert writer.stats()["dropped"]["telemetry"] >= 7
    store.failures = 0
    writer.flush(timeout_s=2)
    assert any(isinstance(r, IncidentRecord) for r in store.written)


def test_stop_flushes_what_is_queued():
    store = FakeStore()
    writer = HistoryWriter(store, max_queue=1000, flush_s=60.0)   # never flushes on its own
    writer.start()
    for i in range(10):
        writer.submit(audit(i))
    writer.stop(timeout_s=2)
    assert len(store.written) == 10


def test_stop_is_bounded_when_the_database_never_answers():
    store = FakeStore(failures=10**9)
    writer = HistoryWriter(store, max_queue=1000, backoff_s=(0.01, 0.01))
    writer.start()
    writer.submit(incident(0))
    import time
    began = time.monotonic()
    writer.stop(timeout_s=0.5)
    assert time.monotonic() - began < 2.0


def test_retention_runs_at_start_and_once_a_day():
    store = FakeStore()
    now = [T0]
    writer = HistoryWriter(store, retention_days=30, clock=lambda: now[0])
    writer.maybe_prune()
    writer.maybe_prune()
    now[0] += 86_400 + 1
    writer.maybe_prune()
    assert store.pruned == [(T0, 30), (T0 + 86_401, 30)]


def test_the_real_store_takes_a_flush(history_store):
    writer = HistoryWriter(history_store)
    writer.submit(incident(0))
    writer.submit(sample(0))
    writer.submit(audit(0))
    assert writer.flush(timeout_s=5)
    counts = history_store.counts()
    assert (counts["incidents"], counts["telemetry_samples"], counts["audit_log"]) == (1, 1, 1)


@pytest.mark.parametrize("bad", [None, "not a record"])
def test_something_that_is_not_a_record_is_ignored(bad):
    writer = HistoryWriter(FakeStore())
    writer.submit(bad)
    assert writer.stats()["queue_depth"] == 0
