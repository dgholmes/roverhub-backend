"""The start-up check that sign-in is not paired with an anonymous broker
(auth/broker_check.py, audit fix-plan 3-01). paho is faked throughout."""
import functools
import logging

import pytest
from fastapi.testclient import TestClient

from auth.broker_check import (
    OVERRIDE_ENV, AnonymousBrokerError, ProbeResult, check_broker_at_startup, override_from_env,
    probe_anonymous,
)


class _FakePaho:
    """Enough of paho 1.6's Client for one probe: connect, loop, disconnect.
    `connack` is the rc the broker answers with, None for no answer;
    `connect_error` makes the TCP dial fail."""

    def __init__(self, client_id, connack=0, connect_error=None, closes=False):
        self.client_id = client_id
        self.connack = connack
        self.connect_error = connect_error
        self.closes = closes
        self.on_connect = None
        self._connect_timeout = 5.0
        self.dialled = None
        self.username = None
        self.subscribed = []
        self.published = []
        self.disconnected = False
        self.loops = 0

    def username_pw_set(self, username, password=None):
        self.username = username

    def connect(self, host, port, keepalive=60):
        if self.connect_error is not None:
            raise self.connect_error
        self.dialled = (host, port)

    def loop(self, timeout=1.0):
        self.loops += 1
        if self.closes:
            return 7  # MQTT_ERR_CONN_LOST
        if self.connack is not None and self.loops == 1:
            self.on_connect(self, None, {}, self.connack)
        return 0

    def subscribe(self, *args, **kwargs):
        self.subscribed.append(args)

    def publish(self, *args, **kwargs):
        self.published.append(args)

    def disconnect(self):
        self.disconnected = True


def _factory(made, **kwargs):
    def make(client_id):
        client = _FakePaho(client_id, **kwargs)
        made.append(client)
        return client
    return make


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.5
        return self.now


# --- the probe ---------------------------------------------------------------

def test_probe_reports_an_accepted_anonymous_client_and_touches_nothing():
    made = []
    result = probe_anonymous("mqtt://localhost:1884", client_factory=_factory(made, connack=0))
    assert result.accepted is True
    client = made[0]
    assert client.dialled == ("localhost", 1884)
    assert client.username is None, "the probe must connect with no username"
    assert client.client_id.startswith("roverhub-anon-probe-")
    assert client.subscribed == [] and client.published == []
    assert client.disconnected
    assert client._connect_timeout == 3.0, "the TCP dial is time-boxed too"


def test_each_probe_uses_a_fresh_client_id():
    made = []
    for _ in range(2):
        probe_anonymous("mqtt://localhost:1884", client_factory=_factory(made, connack=0))
    assert made[0].client_id != made[1].client_id


def test_probe_reports_a_refusal():
    made = []
    result = probe_anonymous("mqtt://broker:1883", client_factory=_factory(made, connack=5))
    assert result.accepted is False and "rc=5" in result.detail
    assert made[0].disconnected


def test_probe_reports_an_unreachable_broker_as_unknown():
    made = []
    result = probe_anonymous("mqtt://localhost:1884",
                             client_factory=_factory(made, connect_error=ConnectionRefusedError("refused")))
    assert result.accepted is None and "could not reach" in result.detail


def test_probe_gives_up_after_the_time_box():
    made = []
    result = probe_anonymous("mqtt://localhost:1884", client_factory=_factory(made, connack=None),
                             timeout_s=3.0, clock=_Clock())
    assert result.accepted is None and "no answer" in result.detail
    assert made[0].loops <= 6 and made[0].disconnected


def test_probe_treats_a_close_without_connack_as_unknown():
    made = []
    result = probe_anonymous("mqtt://localhost:1884", client_factory=_factory(made, closes=True))
    assert result.accepted is None


# --- the start-up decision ---------------------------------------------------

def _probe(accepted, calls=None):
    def probe(url):
        if calls is not None:
            calls.append(url)
        return ProbeResult(accepted, "test broker")
    return probe


def test_refuses_to_start_with_sign_in_on_and_an_anonymous_broker(caplog):
    with caplog.at_level(logging.ERROR, logger="roverhub.auth.broker_check"):
        with pytest.raises(AnonymousBrokerError) as refused:
            check_broker_at_startup(auth_enabled=True, broker_url="mqtt://localhost:1884",
                                    allow_anonymous=False, probe=_probe(True))
    message = str(refused.value)
    assert "docker-compose.auth.yml" in message and OVERRIDE_ENV in message
    assert "Refusing to start" in caplog.text


def test_the_override_starts_with_a_loud_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="roverhub.auth.broker_check"):
        accepted = check_broker_at_startup(auth_enabled=True, broker_url="mqtt://localhost:1884",
                                           allow_anonymous=True, probe=_probe(True))
    assert accepted is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "ADMITS ANONYMOUS CLIENTS" in warnings[0].getMessage()


def test_an_authenticated_broker_starts_quietly(caplog):
    with caplog.at_level(logging.WARNING, logger="roverhub.auth.broker_check"):
        accepted = check_broker_at_startup(auth_enabled=True, broker_url="mqtt://localhost:1884",
                                           allow_anonymous=False, probe=_probe(False))
    assert accepted is False and caplog.records == []


def test_an_unreachable_broker_does_not_refuse_but_says_the_check_did_not_run(caplog):
    with caplog.at_level(logging.WARNING, logger="roverhub.auth.broker_check"):
        accepted = check_broker_at_startup(auth_enabled=True, broker_url="mqtt://localhost:1884",
                                           allow_anonymous=False, probe=_probe(None))
    assert accepted is None
    assert "could not check" in caplog.text


def test_sign_in_off_probes_nothing_and_warns_once(caplog):
    calls = []
    with caplog.at_level(logging.WARNING, logger="roverhub.auth.broker_check"):
        accepted = check_broker_at_startup(auth_enabled=False, broker_url="mqtt://localhost:1884",
                                           allow_anonymous=False, probe=_probe(True, calls))
    assert accepted is None and calls == []
    assert len(caplog.records) == 1 and "SIGN-IN IS OFF" in caplog.records[0].getMessage()


@pytest.mark.parametrize("value, expected", [
    ("true", True), ("1", True), ("YES", True), ("on", True), ("false", False), ("", False), (None, False),
])
def test_override_reads_only_an_explicit_yes(value, expected):
    env = {} if value is None else {OVERRIDE_ENV: value}
    assert override_from_env(env) is expected


# --- wired into the app --------------------------------------------------------

@pytest.fixture
def app_module(monkeypatch):
    import api.app as backend

    monkeypatch.setattr(backend.mqtt_client, "connect", lambda: None)
    monkeypatch.setattr(backend.mqtt_client, "disconnect", lambda: None)
    monkeypatch.setattr(backend, "_scheduler_enabled", lambda: False)
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    return backend


def _probe_with(monkeypatch, backend, accepted):
    """Swap the app's check for the real one with a fake probe."""
    calls = []

    def probe(url):
        calls.append(url)
        return ProbeResult(accepted, "fake broker")

    monkeypatch.setattr(backend, "check_broker_at_startup", functools.partial(check_broker_at_startup, probe=probe))
    return calls


def test_app_refuses_to_start_with_sign_in_on_and_an_anonymous_broker(app_module, monkeypatch):
    monkeypatch.setattr(app_module.auth, "enabled", True)
    calls = _probe_with(monkeypatch, app_module, True)
    connected = []
    monkeypatch.setattr(app_module.mqtt_client, "connect", lambda: connected.append(True))
    with pytest.raises(AnonymousBrokerError):
        with TestClient(app_module.app):
            pass
    assert calls == [app_module._broker_url]
    assert connected == [], "a refused start must not go on to connect the broker link"


def test_app_starts_with_the_override_and_reports_it_on_healthz(app_module, monkeypatch):
    monkeypatch.setattr(app_module.auth, "enabled", True)
    monkeypatch.setenv(OVERRIDE_ENV, "true")
    _probe_with(monkeypatch, app_module, True)
    with TestClient(app_module.app) as client:
        body = client.get("/healthz").json()
    assert body["broker"]["anonymous_accepted_at_start"] is True
    assert isinstance(body["server_time_ms"], int)


def test_app_with_an_unreachable_broker_starts_and_reports_unknown(app_module, monkeypatch):
    monkeypatch.setattr(app_module.auth, "enabled", True)
    _probe_with(monkeypatch, app_module, None)
    with TestClient(app_module.app) as client:
        assert client.get("/healthz").json()["broker"]["anonymous_accepted_at_start"] is None


def test_app_with_sign_in_off_probes_nothing(app_module, monkeypatch):
    calls = _probe_with(monkeypatch, app_module, True)
    with TestClient(app_module.app) as client:
        body = client.get("/healthz").json()
    assert calls == []
    assert body["broker"]["anonymous_accepted_at_start"] is None
