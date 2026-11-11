from __future__ import annotations

import json
import os
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Set, Tuple

from pydantic import ValidationError

from shared.schemas.bridge_status import EdgeHostInfo, EdgeHostMetrics, NavGoalStatus
from shared.schemas.version import SCHEMA_VERSION

logger = logging.getLogger("backend.realtime.mqtt_client")

MqttClientFactory = Callable[[], object]

# paho's MQTT_ERR_SUCCESS. Hardcoded rather than imported because paho is
# deliberately imported lazily (see real_mqtt_client_factory) so the tests can
# run against a fake without the dependency.
MQTT_ERR_SUCCESS = 0


class CommandPublishError(RuntimeError):
    """The command could not be handed to the broker.

    Callers must surface this rather than swallow it: a command that never
    reached the broker will never be acknowledged, so reporting success leaves
    the operator watching "Sending..." forever with no indication that nothing
    was ever sent.
    """


class UnknownRobotError(CommandPublishError):
    """The registry has no record of this robot, so there is no topic to send
    to: the site in the topic comes from the robot's registration.

    This used to publish to `roverhub/unknown/<robot>/commands`, which no bridge
    subscribes to, and report the command as sent -- so an E-Stop issued just
    after a backend restart, before the robot re-registered, went nowhere. A
    CommandPublishError so every caller that already settles an unsent command
    does so for this one too; the HTTP routes answer it with a 404. A robot that
    registered and went offline is still known and is still published to.
    """


def _iso(epoch: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch is not None else None


def real_mqtt_client_factory():
    import paho.mqtt.client as mqtt

    return mqtt.Client()


def _parse_version(value):
    """Dotted version as a tuple of ints, or None if it isn't one. Returning
    None rather than raising keeps an unparseable version a logging problem
    instead of killing the MQTT callback."""
    try:
        return tuple(int(part) for part in str(value).split("."))
    except (AttributeError, TypeError, ValueError):
        return None


class BackendMqttClient:
    """Subscribes to bridge registration, telemetry, state, heartbeat, and
    ack topics, updating the in-memory RobotRegistry and forwarding acks to
    on_ack. Also publishes commands to the bridge. No persistence this
    round."""

    def __init__(
        self,
        broker_url: str,
        registry,
        mqtt_client_factory: MqttClientFactory,
        on_ack: Optional[Callable] = None,
        on_state: Optional[Callable[[str, Optional[str], Optional[str]], None]] = None,
        on_observed: Optional[Callable[[str, str, str, dict], None]] = None,
        on_published: Optional[Callable[..., None]] = None,
        registration_check: Optional[Callable[[str, str, Optional[str]], bool]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._broker_url = broker_url
        self._registry = registry
        self._on_ack = on_ack
        # (bridge_id from the topic, robot_id from the body, the bridge the
        # registry already has for that robot) -> may this registration speak
        # for that robot? auth.Auth.registration_allowed in the app: the broker
        # told it which device key writes each bridge id.
        self._registration_check = registration_check
        self._clock = clock
        # The broker link, for /healthz. paho's thread writes, requests read.
        self._link_lock = threading.Lock()
        self._connected = False
        self._connected_at: Optional[float] = None
        self._connects = 0
        self._disconnects = 0
        self._last_disconnect_at: Optional[float] = None
        self._last_disconnect_rc: Optional[int] = None
        self._last_refused_rc: Optional[int] = None
        self._last_message_at: Optional[float] = None
        # Body/topic disagreements already logged, so a misconfigured bridge
        # at 10 Hz is one warning rather than a flood.
        self._warned: Set[tuple] = set()
        # (robot_id, previous_boot_id, boot_id), after the registry is updated.
        self._on_state = on_state
        # (kind, site_id, robot_id, payload) for the history (history/service.py
        # History.observe): telemetry, state, incident, perception_status. Site
        # and robot from the topic. Called after the registry is updated; it
        # only queues, and anything it raises is contained here.
        self._on_observed = on_observed
        # (robot_id, command, site_id, error) after every publish attempt -- the
        # one choke point every command the backend sends passes, so the audit
        # log hangs off it. error is None when paho took the message.
        self._on_published = on_published
        self._client = mqtt_client_factory()
        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def connect(self) -> None:
        host, port = self._parse_broker_url(self._broker_url)
        # Bound paho's own reconnect backoff. Its network thread reconnects on
        # its own as long as it is alive -- keeping it alive is what _on_message
        # below is about.
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        # With broker sign-in on (infra/mosquitto.auth.conf) this service logs
        # in as the superuser `roverhub-backend`; auth/mqtt_hooks.py checks it.
        service_password = os.environ.get("BROKER_SERVICE_PASSWORD", "")
        if service_password:
            self._client.username_pw_set("roverhub-backend", service_password)
        self._client.connect(host, port)
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish_command(self, robot_id: str, command) -> None:
        record = self._registry.get(robot_id)
        if record is None:
            logger.error(
                "refusing command %s (%s) for robot %s: the registry does not know it, "
                "so it has no site and no topic. The command was NOT sent.",
                command.command_id, command.type, robot_id,
            )
            raise UnknownRobotError("robot not found")
        topic = f"roverhub/{record.site_id}/{robot_id}/commands"
        try:
            info = self._client.publish(topic, command.model_dump_json(), qos=1)
        except Exception as exc:
            self._published(robot_id, command, record.site_id, f"{type(exc).__name__}: {exc}")
            raise
        # publish() never raises -- it reports failure through the returned
        # MQTTMessageInfo. Discarding it (as this did) means a command that was
        # never sent is indistinguishable from one that was, which is how a dead
        # MQTT client turned into commands stuck at "Sending..." indefinitely.
        rc = getattr(info, "rc", MQTT_ERR_SUCCESS)
        if rc != MQTT_ERR_SUCCESS:
            logger.error(
                "could not publish command %s (%s) for robot %s to %s: paho rc=%s. "
                "The command was NOT sent to the edge bridge.",
                command.command_id, command.type, robot_id, topic, rc,
            )
            error = f"could not publish command to the broker (rc={rc}); the robot was not sent this command"
            self._published(robot_id, command, record.site_id, error)
            raise CommandPublishError(error)
        self._published(robot_id, command, record.site_id, None)

    def _published(self, robot_id, command, site_id, error) -> None:
        if self._on_published is None:
            return
        try:
            self._on_published(robot_id, command, site_id=site_id, error=error)
        except Exception:
            logger.exception("the publish observer failed for command %s", command.command_id)

    def _observe(self, kind: str, site_id: str, robot_id: str, data) -> None:
        if self._on_observed is None:
            return
        try:
            self._on_observed(kind, site_id, robot_id, data)
        except Exception:
            logger.exception("the history observer failed on a %s message from %s", kind, robot_id)

    # -- the broker link, for /healthz ------------------------------------------------

    def link_status(self) -> Dict[str, Any]:
        """Cheap (no I/O): whether the broker link is up, how long since any
        message arrived, and how often it has dropped. A dead link otherwise
        shows only as commands that never get an ack."""
        now = self._clock()
        with self._link_lock:
            return {
                "connected": self._connected,
                "connected_since": _iso(self._connected_at) if self._connected else None,
                "last_message_age_s": (round(now - self._last_message_at, 3)
                                       if self._last_message_at is not None else None),
                # Connections after the first: every one is a recovered drop.
                "reconnect_count": max(0, self._connects - 1),
                "disconnect_count": self._disconnects,
                "last_disconnect_at": _iso(self._last_disconnect_at),
                "last_disconnect_rc": self._last_disconnect_rc,
                # The broker's CONNACK refusal (5 = not authorised), if the last
                # attempt was refused; None once a connection succeeds.
                "last_refused_rc": self._last_refused_rc,
            }

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != MQTT_ERR_SUCCESS:
            with self._link_lock:
                self._connected = False
                self._last_refused_rc = rc
            logger.error("the broker refused this backend's connection (rc=%s); paho will retry", rc)
            return
        with self._link_lock:
            self._connected = True
            self._connected_at = self._clock()
            self._connects += 1
            self._last_refused_rc = None
        client.subscribe("roverhub/admin/+/register", qos=1)
        client.subscribe("roverhub/+/+/telemetry", qos=0)
        client.subscribe("roverhub/+/+/state", qos=1)
        client.subscribe("roverhub/+/+/heartbeat", qos=0)
        client.subscribe("roverhub/+/+/ack", qos=1)
        # Perception's (docs/02-system-architecture.md): stored by history/.
        client.subscribe("roverhub/+/+/incidents", qos=1)
        client.subscribe("roverhub/+/+/perception/status", qos=1)

    def _on_disconnect(self, client, userdata, rc) -> None:
        with self._link_lock:
            if self._connected:
                self._disconnects += 1
            self._connected = False
            self._last_disconnect_at = self._clock()
            self._last_disconnect_rc = rc
        if rc == MQTT_ERR_SUCCESS:
            logger.info("MQTT client disconnected cleanly")
            return
        logger.error(
            "unexpected MQTT disconnect (rc=%s). Commands cannot reach any bridge until "
            "the connection is restored; paho will keep retrying.", rc,
        )

    def _on_message(self, client, userdata, message) -> None:
        """paho's callback boundary -- this must never raise.

        paho 1.6 re-raises whatever escapes this callback, and the exception
        propagates out of its network thread, which then dies. That takes down
        message ingestion *and* publishing permanently: nothing reconnects,
        nothing logs, and the REST API keeps returning 200 for commands that are
        silently dropped, because publish() on a dead client still "succeeds"
        from the caller's point of view.

        This is not hypothetical. On 2026-08-11 the backend went silent at
        23:55:42; mosquitto dropped it 90s later (1.5x the 60s keepalive) for
        "exceeded timeout", the robot registry froze at that exact second, and
        every command afterwards sat at "Sending..." forever while telemetry
        kept flowing in the UI -- because the browser subscribes to the broker
        directly and never noticed the backend was gone. Only a restart cleared
        it.

        One malformed message must therefore cost us that message, not the whole
        MQTT subsystem. handle_message stays strict and directly testable; the
        containment lives here, at the boundary paho actually calls.
        """
        with self._link_lock:
            self._last_message_at = self._clock()
        try:
            self.handle_message(message.topic, message.payload)
        except Exception:
            logger.exception(
                "dropping MQTT message on %s: handling it raised. The client stays "
                "connected; this message is lost.", message.topic,
            )

    def handle_message(self, topic: str, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            logger.warning("dropping unparseable MQTT payload on %s", topic)
            return

        if not isinstance(data, dict):
            logger.warning("dropping a non-object MQTT payload on %s", topic)
            return
        parts = topic.split("/")
        if len(parts) == 4 and parts[:2] == ["roverhub", "admin"] and parts[3] == "register":
            self._handle_registration(parts[2], data)
            return

        # Robot and site are the topic's, never the body's: with broker sign-in
        # on, a device key may write only its own robot's topics
        # (auth/mqtt_hooks.py), while a body says whatever its sender wrote.
        if len(parts) == 5 and parts[0] == "roverhub" and parts[3:] == ["perception", "status"]:
            site_id, robot_id, message_kind = parts[1], parts[2], "perception/status"
        elif len(parts) == 4 and parts[0] == "roverhub":
            site_id, robot_id, message_kind = parts[1], parts[2], parts[3]
        else:
            return
        if not self._identity_agrees(topic, site_id, robot_id, message_kind, data):
            return

        if message_kind == "perception/status":
            self._observe("perception_status", site_id, robot_id, data)
        elif message_kind == "incidents":
            # history/ingest.py keeps the payload as sent and files it under the
            # topic's robot and site, warning if the payload names others.
            self._observe("incident", site_id, robot_id, data)
        elif message_kind in ("telemetry", "heartbeat"):
            self._registry.mark_seen(robot_id)
            if message_kind == "heartbeat":
                self._registry.update_host_metrics(
                    robot_id,
                    self._validated(EdgeHostMetrics, data.get("host_metrics"), robot_id, "host_metrics"),
                    at=data.get("timestamp"),
                )
            else:
                self._observe("telemetry", site_id, robot_id, data)
        elif message_kind == "state":
            boot_id = data.get("bridge_boot_id")
            previous = self._registry.update_state(
                robot_id, abstract_state=data["abstract_state"], sdk_state=data["sdk_state"],
                control_owner=data.get("control_owner"), active_run=data.get("active_run"),
                bridge_boot_id=boot_id,
                nav_goal=self._validated(NavGoalStatus, data.get("nav_goal"), robot_id, "nav_goal"),
            )
            if self._on_state is not None:
                self._on_state(robot_id, previous, boot_id)
            self._observe("state", site_id, robot_id, data)
        elif message_kind == "ack" and self._on_ack is not None:
            from shared.schemas.commands import Command

            # _identity_agrees has already refused an ack whose body names
            # another robot; the command store refuses one for a command that
            # was sent to another robot (control/commands.py).
            self._on_ack(Command(**data))

    def _warn_once(self, key: tuple, message: str, *args) -> None:
        if key in self._warned:
            return
        if len(self._warned) < 1024:
            self._warned.add(key)
        logger.warning(message, *args)

    def _identity_agrees(self, topic: str, site_id: str, robot_id: str, kind: str, data: dict) -> bool:
        """Whether a per-robot message may be attributed to the topic's robot
        and site. False (and dropped) when its body names another robot, or
        when the registry has the robot at another site. Incidents keep their
        own rule: filed under the topic, whatever the body says."""
        body_robot = data.get("robot_id")
        if kind not in ("incidents",) and body_robot is not None and body_robot != robot_id:
            self._warn_once(("robot", topic, str(body_robot)),
                            "dropping %s: its body names robot %r but it came on %s's topic. Nothing is "
                            "attributed to either; check that bridge's ROBOT_ID.", topic, body_robot, robot_id)
            return False
        record = self._registry.get(robot_id)
        if record is not None and record.site_id != site_id:
            self._warn_once(("site", topic, record.site_id),
                            "dropping %s: %s is registered at site %r, not %r. Check that device's SITE_ID.",
                            topic, robot_id, record.site_id, site_id)
            return False
        return True

    def _handle_registration(self, bridge_id: str, data: dict) -> None:
        """roverhub/admin/<bridge_id>/register. The body names the robot; the
        topic names the bridge. With broker sign-in on, the broker told us
        which device key writes each bridge id (auth/mqtt_hooks.py), and a
        registration is believed only for that key's robot."""
        robot_id = data["robot_id"]
        body_bridge = data.get("bridge_id")
        if body_bridge is not None and body_bridge != bridge_id:
            self._warn_once(("bridge", bridge_id, str(body_bridge)),
                            "registration on bridge %s's topic names bridge %r; using the topic's",
                            bridge_id, body_bridge)
        if self._registration_check is not None:
            existing = self._registry.get(robot_id)
            if not self._registration_check(bridge_id, robot_id, existing.bridge_id if existing else None):
                self._warn_once(("claim", bridge_id, robot_id),
                                "ignoring a registration for %s on bridge %s's topic: that bridge is not "
                                "written by %s's device key", robot_id, bridge_id, robot_id)
                return
        self._check_schema_version(data)
        self._registry.upsert_from_registration(
            robot_id=robot_id, site_id=data["site_id"], robot_type=data["robot_type"],
            bridge_id=bridge_id,
            schema_version=data.get("schema_version"),
            host=self._validated(EdgeHostInfo, data.get("host"), robot_id, "host"),
            nav_map_id=data.get("nav_map_id") if isinstance(data.get("nav_map_id"), str) else None,
        )

    @staticmethod
    def _validated(model, value, robot_id: str, label: str) -> Optional[dict]:
        """Edge host info/metrics, validated, as a plain dict -- or None.

        Absent (a bridge predating schema 0.0.9) is the ordinary case and is
        silent. Present-but-invalid is logged and dropped: it is display data,
        so it must never cost the registration or heartbeat that carried it --
        a dropped registration takes the robot off GET /robots entirely.
        """
        if value is None:
            return None
        try:
            return model.model_validate(value).model_dump(mode="json")
        except ValidationError as exc:
            logger.warning("ignoring invalid %s from robot %s: %s", label, robot_id, exc)
            return None

    def _check_schema_version(self, data: dict) -> None:
        """Compare the bridge's wire-contract version against ours.

        An edge device can silently run a different schema revision than the
        cloud. That does not fail loudly on its own -- it surfaces as missing or
        None fields and validation errors that look like data bugs, with no
        indication that deployment is the actual cause. This turns it into an
        explicit log line at the one moment we can detect it: the handshake.

        Devices provisioned before shared/ was extracted into its own repo (the
        Jetson, 2026-08-11) carry
        an scp'd copy of shared/ that no `git pull` updates, so drift there is
        especially easy. Newer devices clone it, which narrows the window but
        doesn't close it -- someone can still forget to pull, or edit on-device.

        Deliberately does NOT reject the registration. A version skew usually
        still leaves most fields working, and refusing to register would take a
        robot fully offline over what may be a benign additive change. Warn
        loudly, keep running, let a human decide.
        """
        reported = data.get("schema_version")
        if reported == SCHEMA_VERSION:
            return
        robot_id = data.get("robot_id", "<unknown>")
        if reported is None:
            logger.warning(
                "robot %s registered without a schema_version. Its edge bridge predates "
                "the version handshake (cloud is on %s). Redeploy shared/ to that device "
                "-- see roverhub-edge/scripts/check_edge_sync.sh",
                robot_id, SCHEMA_VERSION,
            )
        else:
            # Which side is behind decides the remedy, and getting this wrong sends
            # someone to fix the wrong machine. The original message always said
            # "redeploy shared/ to the edge device", which is exactly backwards
            # when the edge is the newer one -- as it is right after a schema bump
            # is deployed to devices while the cloud process is still running the
            # version it imported at startup. Python caches modules for the life of
            # a process, so an editable install of shared/ can be correct on disk
            # and stale in memory at the same time.
            logger.error(
                "SCHEMA VERSION MISMATCH for robot %s: edge reports %s, cloud has %s. %s",
                robot_id, reported, SCHEMA_VERSION,
                self._skew_remedy(reported, SCHEMA_VERSION),
            )

    # Kept as a class attribute for existing callers; the logic is module-level
    # so runs/dispatch.py can gate on a schema version too.
    _parse_version = staticmethod(lambda value: _parse_version(value))

    @classmethod
    def _skew_remedy(cls, edge: str, cloud: str) -> str:
        edge_v, cloud_v = cls._parse_version(edge), cls._parse_version(cloud)

        if edge_v is not None and cloud_v is not None and edge_v > cloud_v:
            return (
                "The CLOUD is the older side, so redeploying the edge device will not help. "
                "Usually this backend is simply running a version of shared/ it imported at "
                "startup: restart it. If it persists after a restart, reinstall shared/ here "
                "(pip install -e ../shared). Until then, commands added in the newer schema "
                "are rejected as unwired, and newer fields the edge sends are ignored."
            )
        if edge_v is not None and cloud_v is not None and edge_v < cloud_v:
            return (
                "The EDGE is the older side. Redeploy shared/ to the device and restart the "
                "bridge -- roverhub-edge/scripts/update_edge.sh does both, and "
                "check_edge_sync.sh verifies it. Until then the edge will not understand "
                "commands or fields added in the newer schema."
            )
        return (
            "The two ends disagree on the wire contract; expect missing or malformed "
            "telemetry/command fields. Compare shared/ on both sides -- "
            "roverhub-edge/scripts/check_edge_sync.sh."
        )

    @staticmethod
    def _parse_broker_url(url: str) -> Tuple[str, int]:
        without_scheme = url.split("://", 1)[-1]
        host, _, port = without_scheme.partition(":")
        return host, int(port) if port else 1883
