"""The robot's own log in the console (schema 0.0.22, shared/schemas/logs.py):
POST /robots/{id}/logs/request, the publish behind it, its access rule, and the
broker's rules for the logs and logs/request topics."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.app as backend
from auth.deps import BridgeBindings
from auth.mqtt_hooks import ACC_READ, ACC_SUBSCRIBE, ACC_WRITE
from auth.policy import ROUTES
from auth.tokens import TokenIssuer
from auth.users import UserStore
from realtime.mqtt_client import BackendMqttClient, CommandPublishError, UnknownRobotError
from robots.registry import RobotRegistry
from robots.router import build_robots_router
from shared.schemas.logs import LogRequest

NOW = datetime(2026, 11, 13, 9, 30, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse"


def _app(publish=None, *, online=True):
    registry = RobotRegistry()
    registry.upsert_from_registration("rover-jetson", "site-a", "quad")
    if not online:
        registry.mark_offline("rover-jetson")
    sent = []

    def _publish(robot_id, request):
        sent.append((robot_id, request))

    app = FastAPI()
    app.include_router(build_robots_router(registry, publish_log_request=publish or _publish, clock=lambda: NOW))
    return TestClient(app), sent


# --- the route ----------------------------------------------------------------


def test_a_request_is_published_and_its_id_and_since_returned():
    client, sent = _app()
    response = client.post("/robots/rover-jetson/logs/request", json={"since_hours": 24})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"request_id", "since"}
    (robot_id, request), = sent
    assert robot_id == "rover-jetson"
    assert isinstance(request, LogRequest)
    assert request.request_id == body["request_id"]
    assert len(request.request_id) == 12 and int(request.request_id, 16) >= 0
    assert request.since == body["since"] == "2026-11-12T09:30:00.000+00:00"
    # Readable by the bridge's Python 3.10, which does not take a Z suffix.
    assert datetime.fromisoformat(request.since) == NOW - timedelta(hours=24)
    assert request.sources == ["bridge", "nav"]


def test_fractional_hours_and_one_source():
    client, sent = _app()
    response = client.post("/robots/rover-jetson/logs/request",
                           json={"since_hours": 0.5, "sources": ["bridge", "bridge"]})
    assert response.status_code == 200
    request = sent[0][1]
    assert datetime.fromisoformat(request.since) == NOW - timedelta(minutes=30)
    assert request.sources == ["bridge"]


def test_each_request_gets_its_own_id():
    client, sent = _app()
    ids = {client.post("/robots/rover-jetson/logs/request", json={"since_hours": 1}).json()["request_id"]
           for _ in range(5)}
    assert len(ids) == 5


@pytest.mark.parametrize("body", [
    {"since_hours": 0},
    {"since_hours": -1},
    {"since_hours": 24.0001},
    {"since_hours": "lots"},
    {},
    {"since_hours": 1, "sources": []},
    {"since_hours": 1, "sources": ["bridge", "perception"]},
    {"since_hours": 1, "sources": "bridge"},
])
def test_a_bad_body_is_refused_and_nothing_is_sent(body):
    client, sent = _app()
    assert client.post("/robots/rover-jetson/logs/request", json=body).status_code == 422
    assert sent == []


def test_an_unknown_robot_is_not_found():
    client, sent = _app()
    response = client.post("/robots/ghost/logs/request", json={"since_hours": 1})
    assert response.status_code == 404
    assert sent == []


def test_an_offline_robot_is_a_conflict_said_plainly():
    client, sent = _app(online=False)
    response = client.post("/robots/rover-jetson/logs/request", json={"since_hours": 1})
    assert response.status_code == 409
    assert response.json()["detail"] == "rover-jetson is offline, so its log can't be read"
    assert sent == []


def test_a_broker_that_cannot_take_it_is_unavailable():
    def _fail(robot_id, request):
        raise CommandPublishError("could not send the log request to the broker (rc=4); the robot was not asked")

    client, _ = _app(_fail)
    response = client.post("/robots/rover-jetson/logs/request", json={"since_hours": 1})
    assert response.status_code == 503
    assert "not asked" in response.json()["detail"]


def test_a_robot_forgotten_mid_request_is_not_found():
    def _gone(robot_id, request):
        raise UnknownRobotError("robot not found")

    client, _ = _app(_gone)
    assert client.post("/robots/rover-jetson/logs/request", json={"since_hours": 1}).status_code == 404


# --- the publish ----------------------------------------------------------------


class _Info:
    def __init__(self, rc):
        self.rc = rc


class _Paho:
    def __init__(self):
        self.published = []
        self.publish_rc = 0

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return _Info(self.publish_rc)


def test_the_request_goes_to_the_robots_logs_request_topic_at_qos_1_not_retained():
    registry = RobotRegistry()
    registry.upsert_from_registration("rover-jetson", "site-a", "quad")
    paho = _Paho()
    client = BackendMqttClient("mqtt://localhost:1883", registry, lambda: paho)
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="milliseconds")
    client.publish_log_request("rover-jetson", LogRequest(request_id="abc123", since=since, sources=["nav"]))

    (topic, payload, qos, retain), = paho.published
    assert topic == "roverhub/site-a/rover-jetson/logs/request"
    assert qos == 1 and retain is False
    request = LogRequest.model_validate(json.loads(payload))
    assert request.request_id == "abc123" and request.sources == ["nav"]
    age = datetime.now(timezone.utc) - datetime.fromisoformat(request.since)
    assert timedelta(hours=2) <= age < timedelta(hours=2, minutes=1)


def test_a_request_paho_could_not_take_raises_and_an_unknown_robot_is_refused():
    registry = RobotRegistry()
    registry.upsert_from_registration("rover-jetson", "site-a", "quad")
    paho = _Paho()
    paho.publish_rc = 4  # MQTT_ERR_NO_CONN
    client = BackendMqttClient("mqtt://localhost:1883", registry, lambda: paho)
    request = LogRequest(request_id="abc123", since=NOW.isoformat())
    with pytest.raises(CommandPublishError):
        client.publish_log_request("rover-jetson", request)
    with pytest.raises(UnknownRobotError):
        client.publish_log_request("ghost", request)


def test_the_backend_does_not_subscribe_to_the_log_stream():
    registry = RobotRegistry()
    subscriptions = []

    class _Sub(_Paho):
        def subscribe(self, topic, qos=0):
            subscriptions.append(topic)

    client = BackendMqttClient("mqtt://localhost:1883", registry, _Sub)
    client._on_connect(client._client, None, None, 0)
    assert subscriptions and not any("logs" in topic for topic in subscriptions)


# --- through the real app, sign-in on --------------------------------------------


@pytest.fixture
def auth_on(tmp_path, monkeypatch):
    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    monkeypatch.setattr(auth, "device_keys", {"rover-a": "device-key-a"})
    monkeypatch.setattr(auth, "broker_service_password", "service-pw")
    monkeypatch.setattr(auth, "bridges", BridgeBindings())
    backend.registry._robots.clear()
    backend.registry.upsert_from_registration("rover-a", "site-a", "quad")
    backend.registry.upsert_from_registration("rover-b", "site-b", "quad")
    sent = []
    monkeypatch.setattr(backend.mqtt_client, "publish_log_request",
                        lambda robot_id, request: sent.append((robot_id, request)))
    yield auth, sent
    backend.registry._robots.clear()


def _bearer(client, auth, username, site_roles):
    auth.users.create(username=username, display_name=username, password=PASSWORD,
                      org_role="member", site_roles=site_roles)
    token = client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_the_route_has_the_same_rule_as_reading_a_robot():
    rule = ROUTES[("POST", "/robots/{robot_id}/logs/request")]
    assert rule == ROUTES[("GET", "/robots")]
    assert rule.capabilities == ("watchLive",) and not rule.device and not rule.public


def test_a_viewer_may_ask_for_a_log_at_their_own_site_only(auth_on):
    auth, sent = auth_on
    client = TestClient(backend.app)
    headers = _bearer(client, auth, "vic", {"site-a": "viewer"})
    response = client.post("/robots/rover-a/logs/request", headers=headers, json={"since_hours": 1})
    assert response.status_code == 200
    assert sent[0][0] == "rover-a" and sent[0][1].request_id == response.json()["request_id"]
    assert client.post("/robots/rover-b/logs/request", headers=headers, json={"since_hours": 1}).status_code == 403
    assert len(sent) == 1


def test_signed_out_and_device_keys_may_not_ask(auth_on):
    _, sent = auth_on
    client = TestClient(backend.app)
    assert client.post("/robots/rover-a/logs/request", json={"since_hours": 1}).status_code == 401
    device = {"Authorization": "Bearer device-key-a"}
    assert client.post("/robots/rover-a/logs/request", headers=device, json={"since_hours": 1}).status_code == 403
    assert sent == []


def test_the_real_app_refuses_an_offline_robot(auth_on):
    auth, sent = auth_on
    backend.registry.mark_offline("rover-a")
    client = TestClient(backend.app)
    headers = _bearer(client, auth, "vic", {"site-a": "viewer"})
    response = client.post("/robots/rover-a/logs/request", headers=headers, json={"since_hours": 1})
    assert response.status_code == 409
    assert response.json()["detail"] == "rover-a is offline, so its log can't be read"
    assert sent == []


# --- the broker -------------------------------------------------------------------


def _acl(client, username, topic, acc):
    return client.post("/mqtt/acl", json={"username": username, "clientid": "c", "topic": topic, "acc": acc}).status_code


def test_a_browser_reads_the_logs_of_its_own_sites_and_never_the_requests(auth_on):
    auth, _ = auth_on
    user = auth.users.create(username="vic", display_name="vic", password=PASSWORD,
                             org_role="member", site_roles={"site-a": "viewer"})
    client = TestClient(backend.app)
    uid = user.user_id
    assert _acl(client, uid, "roverhub/+/+/logs", ACC_SUBSCRIBE) == 200
    assert _acl(client, uid, "roverhub/site-a/rover-a/logs", ACC_SUBSCRIBE) == 200
    assert _acl(client, uid, "roverhub/site-a/rover-a/logs", ACC_READ) == 200
    assert _acl(client, uid, "roverhub/site-b/rover-b/logs", ACC_READ) == 403
    assert _acl(client, uid, "roverhub/site-b/rover-b/logs", ACC_SUBSCRIBE) == 403
    assert _acl(client, uid, "roverhub/site-a/rover-a/logs", ACC_WRITE) == 403
    # Asking goes through POST /robots/{id}/logs/request, never the broker.
    assert _acl(client, uid, "roverhub/site-a/rover-a/logs/request", ACC_WRITE) == 403
    assert _acl(client, uid, "roverhub/site-a/rover-a/logs/request", ACC_SUBSCRIBE) == 403
    assert _acl(client, uid, "roverhub/site-a/rover-a/logs/#", ACC_SUBSCRIBE) == 403


def test_a_device_publishes_its_own_log_and_reads_only_its_own_requests(auth_on):
    client = TestClient(backend.app)
    assert _acl(client, "rover-a", "roverhub/site-a/rover-a/logs", ACC_WRITE) == 200
    assert _acl(client, "rover-a", "roverhub/site-a/rover-a/logs/request", ACC_SUBSCRIBE) == 200
    assert _acl(client, "rover-a", "roverhub/site-a/rover-a/logs/request", ACC_READ) == 200
    # Nobody but the backend writes a request, a device included.
    assert _acl(client, "rover-a", "roverhub/site-a/rover-a/logs/request", ACC_WRITE) == 403
    # Not another robot's, and not under another site than its registration's.
    assert _acl(client, "rover-a", "roverhub/site-b/rover-b/logs", ACC_WRITE) == 403
    assert _acl(client, "rover-a", "roverhub/site-b/rover-b/logs/request", ACC_SUBSCRIBE) == 403
    assert _acl(client, "rover-a", "roverhub/site-b/rover-a/logs", ACC_WRITE) == 403
    assert _acl(client, "rover-a", "roverhub/site-a/+/logs/request", ACC_SUBSCRIBE) == 403
    assert _acl(client, "rover-a", "roverhub/site-a/rover-a/logs/+", ACC_SUBSCRIBE) == 403


def test_the_backend_logs_in_as_superuser_so_it_may_send_requests(auth_on):
    client = TestClient(backend.app)
    assert client.post("/mqtt/auth", json={"username": "roverhub-backend", "password": "service-pw"}).status_code == 200
    assert client.post("/mqtt/superuser", json={"username": "roverhub-backend"}).status_code == 200
