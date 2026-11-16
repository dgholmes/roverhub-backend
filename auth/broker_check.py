"""Start-up check: with sign-in on, the broker must not admit anonymous clients.

Sign-in on the backend protects the REST routes only. Commands reach a robot
through the broker (roverhub/{site}/{robot}/commands), so a broker that admits
anonymous clients -- the plain dev Mosquitto, infra/mosquitto.conf -- lets
anyone on the network drive a robot while the console asks everyone else to
sign in. Audit fix-plan 3-01: the authenticated broker (docker-compose.auth.yml)
is the only setup sign-in is meant for, so the backend refuses to start with
sign-in on against a broker that lets an anonymous client in.

How it is checked: one short connection with no username and a random client
id, time-boxed, then a disconnect. Nothing is subscribed or published. The
answer is the broker's CONNACK:

- accepted (rc 0): refuse to start, unless AUTH_ALLOW_ANONYMOUS_BROKER is true
  -- a deliberate dev setup, which is then warned about loudly on every start;
- refused (any other rc): fine;
- no answer (broker down, wrong URL, timed out): not a reason to refuse. The
  backend's own link retries the broker (realtime/mqtt_client.py); this check
  logs that it could not run and /healthz reports `null`.

With the authenticated broker the refusal comes from mosquitto-go-auth, which
asks this backend -- not yet listening during start-up, so it denies at once.

With sign-in off nothing is probed: everything is open by design, and one
warning says so.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

logger = logging.getLogger("roverhub.auth.broker_check")

OVERRIDE_ENV = "AUTH_ALLOW_ANONYMOUS_BROKER"
PROBE_TIMEOUT_S = 3.0

# A CONNACK's return code: 0 accepted. Hardcoded, like realtime/mqtt_client.py,
# so the tests need no paho.
_CONNACK_ACCEPTED = 0


@dataclass(frozen=True)
class ProbeResult:
    """`accepted`: True the broker admitted an anonymous client, False it
    refused one, None no answer. `detail` says what happened, for the log."""
    accepted: Optional[bool]
    detail: str


class AnonymousBrokerError(RuntimeError):
    """Sign-in is on and the broker admits anonymous clients. Raised from the
    start-up hook, so the backend does not start."""


def _parse_broker_url(url: str) -> Tuple[str, int]:
    # The same reading as BackendMqttClient._parse_broker_url, so the probe
    # dials exactly the broker the backend will use.
    without_scheme = url.split("://", 1)[-1]
    host, _, port = without_scheme.partition(":")
    return host, int(port) if port else 1883


def _paho_client(client_id: str):
    import paho.mqtt.client as mqtt

    return mqtt.Client(client_id=client_id, clean_session=True)


def probe_anonymous(broker_url: str, *, client_factory: Callable[[str], object] = _paho_client,
                    timeout_s: float = PROBE_TIMEOUT_S, clock: Callable[[], float] = time.monotonic) -> ProbeResult:
    """Does the broker at `broker_url` admit a client with no username?

    One connection: dial, read until the CONNACK or `timeout_s`, disconnect.
    No username, no subscription, no publish. Run on a daemon thread and waited
    for at most `timeout_s` (+0.5 s), because a refused dial to `localhost` on
    Windows tries ::1 and then 127.0.0.1, each up to its own timeout -- 4 s
    measured -- and start-up should not wait for that."""
    outcome: list = []

    def run():
        outcome.append(_probe_once(broker_url, client_factory, timeout_s, clock))

    worker = threading.Thread(target=run, name="anon-broker-probe", daemon=True)
    worker.start()
    worker.join(timeout_s + 0.5)
    if outcome:
        return outcome[0]
    return ProbeResult(None, f"{broker_url} gave no answer within {timeout_s:g} s")


def _probe_once(broker_url: str, client_factory: Callable[[str], object], timeout_s: float,
                clock: Callable[[], float]) -> ProbeResult:
    """The probe itself, on the calling thread (no paho network thread is
    started, so nothing is left running)."""
    host, port = _parse_broker_url(broker_url)
    client = client_factory(f"roverhub-anon-probe-{uuid.uuid4().hex[:12]}")
    answer: dict = {}

    def on_connect(_client, _userdata, _flags, rc):
        answer["rc"] = rc

    client.on_connect = on_connect
    # Bound the TCP dial too: paho 1.6 has no public setter, and a broker
    # address that drops packets would otherwise hold start-up for its 5 s.
    if hasattr(client, "_connect_timeout"):
        client._connect_timeout = timeout_s
    deadline = clock() + timeout_s
    try:
        client.connect(host, port, keepalive=max(5, int(timeout_s) + 2))
    except Exception as exc:  # refused TCP, no such host, timed out
        return ProbeResult(None, f"could not reach {host}:{port} ({type(exc).__name__}: {exc})")
    try:
        while "rc" not in answer and clock() < deadline:
            if client.loop(timeout=0.1) != 0:
                break  # the broker closed the connection without a CONNACK
    except Exception as exc:
        return ProbeResult(None, f"the connection to {host}:{port} failed ({type(exc).__name__}: {exc})")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    if "rc" not in answer:
        return ProbeResult(None, f"{host}:{port} gave no answer within {timeout_s:g} s")
    rc = answer["rc"]
    if rc == _CONNACK_ACCEPTED:
        return ProbeResult(True, f"{host}:{port} accepted a client with no username")
    return ProbeResult(False, f"{host}:{port} refused a client with no username (CONNACK rc={rc})")


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def override_from_env(environ) -> bool:
    return _truthy(environ.get(OVERRIDE_ENV))


def check_broker_at_startup(*, auth_enabled: bool, broker_url: str, allow_anonymous: bool,
                            probe: Callable[[str], ProbeResult] = probe_anonymous) -> Optional[bool]:
    """The start-up decision. Returns whether the broker admitted an anonymous
    client (None: not checked, or no answer), for /healthz. Raises
    AnonymousBrokerError when sign-in is on, the broker admits anonymous
    clients and the override is not set."""
    if not auth_enabled:
        logger.warning(
            "SIGN-IN IS OFF (AUTH_ENABLED is not true): anyone who can reach this backend or the "
            "MQTT broker at %s can send commands to the robots. Development only; see "
            "docs/superpowers/specs/2026-11-04-sign-in-design.md to turn it on.", broker_url)
        return None
    result = probe(broker_url)
    if result.accepted is None:
        logger.warning(
            "could not check whether the MQTT broker admits anonymous clients: %s. Sign-in is on; "
            "if this broker is the plain dev one (infra/mosquitto.conf), anyone on the network can "
            "send commands to the robots. The backend starts anyway and keeps retrying the broker.",
            result.detail)
        return None
    if not result.accepted:
        logger.info("MQTT broker refuses anonymous clients (%s), as sign-in needs", result.detail)
        if allow_anonymous:
            logger.info("%s is set but not needed: the broker already refuses anonymous clients", OVERRIDE_ENV)
        return False
    if allow_anonymous:
        logger.warning(
            "!!! THE MQTT BROKER ADMITS ANONYMOUS CLIENTS (%s) while sign-in is on. Anyone on the "
            "network can publish to a robot's command topic without signing in. Starting anyway because "
            "%s=true -- development only. Use the authenticated broker "
            "(docker compose -f docker-compose.yml -f docker-compose.auth.yml up -d) and remove %s "
            "before anyone else is on this network. !!!", result.detail, OVERRIDE_ENV, OVERRIDE_ENV)
        return True
    message = (
        f"Refusing to start: sign-in is on (AUTH_ENABLED=true) but the MQTT broker admits anonymous "
        f"clients ({result.detail}). Robots take their commands from the broker, so anyone on the network "
        f"could still drive them without signing in. Fix: run the authenticated broker -- from the root "
        f"repo, docker compose -f docker-compose.yml -f docker-compose.auth.yml up -d -- with "
        f"BROKER_SERVICE_PASSWORD and DEVICE_KEYS set (docs/superpowers/specs/2026-11-04-sign-in-design.md). "
        f"For a deliberate dev setup on the plain broker, set {OVERRIDE_ENV}=true in roverhub-backend/.env."
    )
    logger.error(message)
    raise AnonymousBrokerError(message)
