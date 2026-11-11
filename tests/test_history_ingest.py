"""history/ingest.py: what an MQTT message becomes in the history, decided on
paho's thread without touching the database."""
import pytest

from history.ingest import PerceptionTracker, TelemetryDecimator, incident_record

T0 = 1_762_840_800.0


def telemetry(battery=80.0, vx=0.0, freshness="live", position=True):
    frame = {"robot_id": "rover-a", "site_id": "site-a", "battery_percent": battery,
             "vel_body": [vx, 0.0, 0.0], "pos_body": [9.0, 9.0, 0.0], "grpc_freshness": freshness}
    if position:
        frame["position"] = {"x_m": 1.5, "y_m": -2.0, "heading_deg": 45.0, "quality": "localized",
                             "provider": "slam", "freshness": "live", "updated_at": "t"}
    return frame


def state(abstract="STAND", posture="upright", owner=None, run=None, sdk="balance_stand"):
    return {"robot_id": "rover-a", "site_id": "site-a", "abstract_state": abstract, "sdk_state": sdk,
            "posture": posture, "control_owner": owner,
            "active_run": {"run_id": run} if run else None}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def dec():
    clock = Clock()
    d = TelemetryDecimator(interval_s=5.0, clock=clock)
    d.clock = clock
    return d


def test_the_first_message_writes_a_row_and_then_one_per_interval(dec):
    first = dec.on_telemetry("site-a", "rover-a", telemetry())
    assert first is not None and first.reason == "first"
    assert (first.x_m, first.y_m, first.heading_deg, first.position_quality) == (1.5, -2.0, 45.0, "localized")
    for _ in range(40):                       # 10 Hz for 4 s: nothing more
        dec.clock.t += 0.1
        assert dec.on_telemetry("site-a", "rover-a", telemetry()) is None
    dec.clock.t += 1.5                        # past 5 s since the first row
    row = dec.on_telemetry("site-a", "rover-a", telemetry())
    assert row is not None and row.reason == "interval"


@pytest.mark.parametrize("change", [
    {"abstract": "MANUAL"}, {"posture": "fallen"}, {"owner": "ana"}, {"abstract": "E_STOP", "sdk": "passive"},
])
def test_a_state_change_writes_a_row_at_once(dec, change):
    dec.on_state("site-a", "rover-a", state())
    dec.on_telemetry("site-a", "rover-a", telemetry())
    dec.clock.t += 0.5
    row = dec.on_state("site-a", "rover-a", state(**change))
    assert row is not None and row.reason == "change"


def test_a_new_active_run_alone_is_not_a_trigger_but_is_recorded(dec):
    dec.on_state("site-a", "rover-a", state())
    dec.clock.t += 0.5
    assert dec.on_state("site-a", "rover-a", state(run="run-1")) is None
    dec.clock.t += 5.0
    assert dec.on_telemetry("site-a", "rover-a", telemetry()).active_run_id == "run-1"


def test_a_link_change_writes_a_row_at_once(dec):
    dec.on_telemetry("site-a", "rover-a", telemetry())
    dec.clock.t += 0.5
    assert dec.on_telemetry("site-a", "rover-a", telemetry(freshness="stale")).reason == "change"


def test_crossing_a_5_percent_battery_bucket_writes_a_row(dec):
    dec.on_telemetry("site-a", "rover-a", telemetry(battery=80.4))
    dec.clock.t += 0.5
    assert dec.on_telemetry("site-a", "rover-a", telemetry(battery=80.1)) is None
    dec.clock.t += 0.5
    assert dec.on_telemetry("site-a", "rover-a", telemetry(battery=79.9)).reason == "change"


def test_an_unknown_battery_is_stored_as_unknown_never_zero(dec):
    row = dec.on_telemetry("site-a", "rover-a", telemetry(battery=None))
    assert row.battery_pct is None
    dec.clock.t += 0.5
    # Unknown -> known is a change of bucket too.
    assert dec.on_telemetry("site-a", "rover-a", telemetry(battery=50.0)).battery_pct == 50.0


def test_speed_comes_from_body_velocity_and_old_bridges_without_position_leave_it_empty(dec):
    row = dec.on_telemetry("site-a", "rover-a", telemetry(vx=0.3, position=False))
    assert row.speed_mps == pytest.approx(0.3)
    assert (row.x_m, row.y_m, row.position_quality) == (None, None, None)


def test_robots_are_decimated_separately(dec):
    assert dec.on_telemetry("site-a", "rover-a", telemetry()) is not None
    assert dec.on_telemetry("site-a", "rover-b", telemetry()) is not None


def test_perception_status_writes_on_change_only():
    tracker = PerceptionTracker(clock=lambda: T0)
    status = {"robot_id": "rover-a", "site_id": "site-a", "at": "t", "camera": "front",
              "source_state": "live", "fps": 9.5,
              "detectors": [{"detector_id": "person", "kind": "model", "version": "1", "labels": ["person"],
                             "status": "running"}]}
    assert tracker.on_status("site-a", "rover-a", status) is not None
    assert tracker.on_status("site-a", "rover-a", {**status, "fps": 8.0}) is None   # fps alone: no row
    assert tracker.on_status("site-a", "rover-a", {**status, "source_state": "unavailable"}) is not None


def test_an_incident_takes_its_robot_and_site_from_the_topic():
    payload = {"incident_id": "i-1", "robot_id": "someone-else", "site_id": "site-z", "rule_id": "r",
               "label": "fallen_person", "severity": "critical", "opened_at": "2026-11-11T06:00:00Z",
               "camera": "front", "detections": [], "message": "m"}
    record = incident_record("site-a", "rover-a", payload, T0)
    assert (record.robot_id, record.site_id) == ("rover-a", "site-a")


def test_an_invalid_incident_is_dropped():
    assert incident_record("site-a", "rover-a", {"incident_id": "i-1"}, T0) is None
