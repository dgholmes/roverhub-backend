"""The real app with history on (SQLite in the test folder, conftest.py) and a
fake broker: messages arrive as paho would hand them over, and the console's
routes see what was stored."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import api.app as backend
from auth.tokens import TokenIssuer
from auth.users import UserStore

PASSWORD = "correct horse"


class _Published:
    rc = 0
    mid = 1


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(backend.mqtt_client, "connect", lambda: None)
    monkeypatch.setattr(backend.mqtt_client, "disconnect", lambda: None)
    sent = []
    monkeypatch.setattr(backend.mqtt_client._client, "publish",
                        lambda topic, payload, qos=0: sent.append((topic, payload)) or _Published())
    backend.registry._robots.clear()
    backend.registry.upsert_from_registration("rover-e2e", "site-a", "quad")
    with TestClient(backend.app) as client:
        client.sent = sent
        yield client
    backend.registry._robots.clear()


def _deliver(topic, payload):
    backend.mqtt_client.handle_message(topic, json.dumps(payload).encode())


def _flush():
    assert backend.history.flush(timeout_s=5)


def test_an_incident_is_stored_acknowledged_resolved_and_audited(app):
    iid = f"rover-e2e-{uuid.uuid4().hex[:8]}"
    _deliver("roverhub/site-a/rover-e2e/incidents", {
        "incident_id": iid, "robot_id": "rover-e2e", "site_id": "site-a", "rule_id": "builtin.fall_alert",
        "label": "fallen_person", "severity": "critical", "opened_at": "2026-11-11T06:00:00Z",
        "camera": "front", "detections": [], "evidence_snapshot_ids": [], "evidence_pending": True,
        "message": "Fallen person detected"})
    _flush()

    listed = app.get("/incidents", params={"robot_id": "rover-e2e"}).json()["items"]
    assert iid in [i["incident_id"] for i in listed]
    assert app.post(f"/incidents/{iid}/acknowledge").status_code == 200
    assert app.post(f"/incidents/{iid}/resolve", json={"outcome": "confirmed", "note": "real"}).status_code == 200
    _flush()

    rows = app.get("/audit", params={"target": iid}).json()["items"]
    assert [(r["action"], r["actor"]) for r in rows] == [
        ("incident.resolve", "operator-dev"), ("incident.acknowledge", "operator-dev")]


def test_a_command_is_audited_with_the_server_side_actor_and_its_outcome(app):
    response = app.post("/robots/rover-e2e/commands",
                        json={"type": "SET_STATE", "label": "Stand", "params": {"target": "balance_stand"},
                              "initiated_by": "mallory"})
    assert response.status_code == 200
    command = response.json()
    _deliver("roverhub/site-a/rover-e2e/ack", {**command, "current_stage": "execution_completed"})
    _flush()

    rows = [r for r in app.get("/audit", params={"target": "rover-e2e"}).json()["items"]
            if r["detail"].get("command_id") == command["command_id"]]
    assert [(r["action"], r["actor"], r["outcome"]) for r in rows] == [
        ("command.outcome", "operator-dev", "execution_completed"),
        ("command.publish", "operator-dev", "sent"),
    ]


def test_telemetry_becomes_history(app):
    _deliver("roverhub/site-a/rover-e2e/telemetry", {
        "robot_id": "rover-e2e", "site_id": "site-a", "battery_percent": None, "vel_body": [0.2, 0, 0],
        "grpc_freshness": "live"})
    _flush()
    samples = app.get("/robots/rover-e2e/history").json()["samples"]
    assert samples and samples[-1]["battery_pct"] is None and samples[-1]["speed_mps"] == pytest.approx(0.2)


def test_evidence_uploaded_with_the_incident_id_is_linked(app):
    iid = f"rover-e2e-{uuid.uuid4().hex[:8]}"
    upload = app.post("/robots/rover-e2e/snapshots", data={"camera": "front", "incident_id": iid},
                      files={"file": ("evidence.jpg", b"\xff\xd8\xff\xe0jpeg", "image/jpeg")})
    assert upload.status_code == 200
    _deliver("roverhub/site-a/rover-e2e/incidents", {
        "incident_id": iid, "robot_id": "rover-e2e", "site_id": "site-a", "rule_id": "r",
        "label": "person_lying", "severity": "warning", "opened_at": "2026-11-11T06:00:00Z",
        "camera": "front", "detections": [], "evidence_pending": True, "message": "m"})
    _flush()
    got = app.get(f"/incidents/{iid}").json()
    assert [e["url"] for e in got["evidence"]] == [f"/snapshots/{upload.json()['snapshot_id']}"]


def test_perception_status_is_recorded(app):
    _deliver("roverhub/site-a/rover-e2e/perception/status", {
        "robot_id": "rover-e2e", "site_id": "site-a", "at": "2026-11-11T06:00:00Z", "camera": "front",
        "source_state": "live", "fps": 9.0, "detectors": []})
    _flush()
    assert app.get("/history/status").json()["rows"]["perception_status"] >= 1


# -- who may do what, with sign-in on ---------------------------------------------------

@pytest.fixture
def signed_in(tmp_path, monkeypatch, app):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    for name, role in (("vic", "viewer"), ("ana", "operator"), ("sue", "supervisor"), ("eng", "engineer")):
        auth.users.create(username=name, display_name=name, password=PASSWORD, org_role="member",
                          site_roles={"site-a": role})
    # An auditor: watch-only at the site, with the audit-access flag.
    auth.users.create(username="aud", display_name="aud", password=PASSWORD, org_role="member",
                      site_roles={"site-a": "viewer"}, audit_access=True)

    def headers(name):
        token = app.post("/auth/login", json={"username": name, "password": PASSWORD}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}
    return headers


def test_viewers_read_incidents_operators_answer_them_managers_read_the_audit(app, signed_in):
    iid = f"rover-e2e-{uuid.uuid4().hex[:8]}"
    _deliver("roverhub/site-a/rover-e2e/incidents", {
        "incident_id": iid, "robot_id": "rover-e2e", "site_id": "site-a", "rule_id": "r",
        "label": "fallen_person", "severity": "critical", "opened_at": "2026-11-11T06:00:00Z",
        "camera": "front", "detections": [], "message": "m"})
    _flush()
    vic, ana, sue, eng = (signed_in(n) for n in ("vic", "ana", "sue", "eng"))

    assert app.get(f"/incidents/{iid}", headers=vic).status_code == 200
    assert app.get("/robots/rover-e2e/history", headers=vic).status_code == 200
    assert app.post(f"/incidents/{iid}/acknowledge", headers=vic).status_code == 403
    acked = app.post(f"/incidents/{iid}/acknowledge", headers=ana)
    assert acked.status_code == 200 and acked.json()["acknowledged_by"] != "operator-dev"

    assert app.get("/audit", headers=vic).status_code == 403
    assert app.get("/audit", headers=ana).status_code == 403
    assert app.get("/audit", headers=sue).status_code == 200
    assert app.get("/audit", headers=eng).status_code == 200
    aud = signed_in("aud")
    assert app.get("/audit", headers=aud).status_code == 200
    _flush()
    # Sign-ins belong to no site: the auditor reads them, a site supervisor does not.
    logins = app.get("/audit", headers=aud, params={"action": "auth.login", "limit": 500}).json()["items"]
    assert {("vic", "ok"), ("ana", "ok"), ("sue", "ok"), ("eng", "ok")} <= {
        (r["detail"]["username"], r["outcome"]) for r in logins}
    assert all(r["actor"].startswith("user-") for r in logins if r["outcome"] == "ok")
    assert app.get("/audit", headers=sue, params={"action": "auth.login"}).json()["items"] == []
    # The site's own rows are the supervisor's: the acknowledgement, with its robot.
    acks = app.get("/audit", headers=sue, params={"action": "incident.*", "robot_id": "rover-e2e"}).json()["items"]
    assert [(r["target"], r["site_id"]) for r in acks if r["target"] == iid] == [(iid, "site-a")]
