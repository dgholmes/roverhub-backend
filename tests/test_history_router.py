"""history/router.py: the incident, history, audit and status routes, on both
engines, and what they answer when history could not start."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from history.records import AuditRecord, IncidentRecord, SampleRecord
from history.router import build_history_router
from history.service import History

T0 = 1_762_840_800.0


def incident(iid="i-1", evidence=()):
    payload = {"incident_id": iid, "robot_id": "rover-a", "site_id": "site-a", "rule_id": "r",
               "label": "fallen_person", "severity": "critical", "opened_at": "2026-11-11T06:00:00Z",
               "camera": "front", "detections": [], "message": "m", "evidence_snapshot_ids": list(evidence)}
    return IncidentRecord(payload=payload, robot_id="rover-a", site_id="site-a", received_at=T0)


def sample(i, robot="rover-a"):
    return SampleRecord(robot_id=robot, site_id="site-a", at=T0 + i, battery_pct=None, x_m=1.0, y_m=1.0,
                        heading_deg=0.0, position_quality="estimated", speed_mps=0.1, abstract_state="STAND",
                        sdk_state="walk", posture="upright", control_owner=None, active_run_id=None,
                        link="live", reason="interval")


@pytest.fixture
def env(history_store):
    history = History(store=history_store, engine=history_store.db.dialect)
    linked = {"i-1": ["rover-a-11111111-2222-3333-4444-555555555555.jpg"]}
    app = FastAPI()
    app.include_router(build_history_router(
        history, evidence_for=lambda iid: linked.get(iid, []),
        robot_known=lambda rid: rid == "rover-known", clock=lambda: T0 + 100))
    history.client = TestClient(app)
    return history


def test_incidents_are_listed_and_fetched_with_their_evidence(env):
    env.store.write([incident("i-1", evidence=["rover-a-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jpg"]), incident("i-2")])
    page = env.client.get("/incidents", params={"robot_id": "rover-a", "limit": 1}).json()
    assert [i["incident_id"] for i in page["items"]] == ["i-2"] and page["next_cursor"]
    rest = env.client.get("/incidents", params={"cursor": page["next_cursor"]}).json()
    assert [i["incident_id"] for i in rest["items"]] == ["i-1"]

    got = env.client.get("/incidents/i-1").json()
    # Named by the payload, and linked by an upload that named the incident.
    assert [e["url"] for e in got["evidence"]] == [
        "/snapshots/rover-a-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jpg",
        "/snapshots/rover-a-11111111-2222-3333-4444-555555555555.jpg",
    ]
    assert env.client.get("/incidents/nope").status_code == 404


def test_acknowledge_and_resolve_follow_the_state_machine(env):
    env.store.write([incident()])
    acked = env.client.post("/incidents/i-1/acknowledge")
    assert acked.status_code == 200 and acked.json()["state"] == "acknowledged"
    assert acked.json()["acknowledged_by"] == "operator-dev"
    assert env.client.post("/incidents/i-1/acknowledge").json() == acked.json()

    body = {"outcome": "false_alarm", "note": "a coat on a chair"}
    done = env.client.post("/incidents/i-1/resolve", json=body)
    assert done.status_code == 200 and done.json()["outcome"] == "false_alarm"
    assert env.client.post("/incidents/i-1/resolve", json=body).status_code == 200

    refused = env.client.post("/incidents/i-1/resolve", json={"outcome": "confirmed"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "incident i-1 was already resolved as false_alarm by operator-dev; it cannot be resolved again")
    assert env.client.post("/incidents/i-1/acknowledge").status_code == 409


def test_resolve_needs_a_known_outcome(env):
    env.store.write([incident()])
    assert env.client.post("/incidents/i-1/resolve", json={"outcome": "maybe"}).status_code == 422
    assert env.client.post("/incidents/i-1/resolve", json={}).status_code == 422
    assert env.client.post("/incidents/nope/resolve", json={"outcome": "other"}).status_code == 404


def test_a_robots_history_is_thinned_to_max_points(env):
    env.store.write([sample(i) for i in range(300)])
    body = env.client.get("/robots/rover-a/history",
                          params={"from": "2025-11-11T06:00:00Z", "to": "2025-11-11T06:05:00Z",
                                  "max_points": 50}).json()
    assert body["robot_id"] == "rover-a" and 25 <= len(body["samples"]) <= 50
    assert body["samples"][0]["battery_pct"] is None
    assert env.client.get("/robots/rover-a/history", params={"max_points": 9999}).status_code == 422


def test_history_for_a_robot_nobody_knows_is_404_but_a_known_one_is_empty(env):
    assert env.client.get("/robots/ghost/history").status_code == 404
    known = env.client.get("/robots/rover-known/history")
    assert known.status_code == 200 and known.json()["samples"] == []


def test_the_audit_log_is_listed(env):
    env.store.write([AuditRecord(at=T0 + i, actor="ana", action="command.publish", target="rover-a",
                                 outcome="sent", detail={}) for i in range(3)])
    page = env.client.get("/audit", params={"actor": "ana", "limit": 2}).json()
    assert len(page["items"]) == 2 and page["items"][0]["at"] == "2025-11-11T06:00:02Z"
    assert page["next_cursor"]


def test_status_reports_the_engine_and_the_counters(env):
    env.store.write([incident()])
    status = env.client.get("/history/status").json()
    assert status["available"] is True
    assert status["engine"] == env.store.db.dialect
    assert status["migration"] >= 1
    assert status["rows"]["incidents"] == 1
    assert status["queue_depth"] == 0 and status["dropped"] == {}


@pytest.mark.parametrize("method, path", [
    ("GET", "/incidents"), ("GET", "/incidents/i-1"), ("POST", "/incidents/i-1/acknowledge"),
    ("GET", "/robots/rover-a/history"), ("GET", "/audit"), ("GET", "/history/status"),
])
def test_every_route_says_plainly_that_history_is_off(method, path):
    history = History.unavailable("HISTORY_DB points at a folder that does not exist")
    app = FastAPI()
    app.include_router(build_history_router(history))
    response = TestClient(app).request(method, path)
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "history is not available on this backend (HISTORY_DB points at a folder that does not exist); "
        "see the backend log")
