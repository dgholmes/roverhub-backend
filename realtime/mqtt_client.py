from __future__ import annotations

import json
import logging
from typing import Callable, Optional, Tuple

from pydantic import ValidationError

from shared.schemas.bridge_status import EdgeHostInfo, EdgeHostMetrics
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
    ):
        self._broker_url = broker_url
        self._registry = registry
        self._on_ack = on_ack
        # (robot_id, previous_boot_id, boot_id), after the registry is updated.
        self._on_state = on_state
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
        self._client.connect(host, port)
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish_command(self, robot_id: str, command) -> None:
        record = self._registry.get(robot_id)
        site_id = record.site_id if record is not None else "unknown"
        topic = f"roverhub/{site_id}/{robot_id}/commands"
        info = self._client.publish(topic, command.model_dump_json(), qos=1)
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
            raise CommandPublishError(
                f"could not publish command to the broker (rc={rc}); the robot was not sent this command"
            )

    def _on_connect(self, client, userdata, flags, rc) -> None:
        client.subscribe("roverhub/admin/+/register", qos=1)
        client.subscribe("roverhub/+/+/telemetry", qos=0)
        client.subscribe("roverhub/+/+/state", qos=1)
        client.subscribe("roverhub/+/+/heartbeat", qos=0)
        client.subscribe("roverhub/+/+/ack", qos=1)

    def _on_disconnect(self, client, userdata, rc) -> None:
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

        parts = topic.split("/")
        if topic.startswith("roverhub/admin/") and topic.endswith("/register"):
            self._check_schema_version(data)
            self._registry.upsert_from_registration(
                robot_id=data["robot_id"], site_id=data["site_id"], robot_type=data["robot_type"],
                bridge_id=data.get("bridge_id"),
                schema_version=data.get("schema_version"),
                host=self._validated(EdgeHostInfo, data.get("host"), data["robot_id"], "host"),
            )
            return

        if len(parts) != 4 or parts[0] != "roverhub":
            return
        message_kind = parts[3]

        if message_kind in ("telemetry", "heartbeat"):
            self._registry.mark_seen(data["robot_id"])
            if message_kind == "heartbeat":
                self._registry.update_host_metrics(
                    data["robot_id"],
                    self._validated(EdgeHostMetrics, data.get("host_metrics"), data["robot_id"], "host_metrics"),
                    at=data.get("timestamp"),
                )
        elif message_kind == "state":
            boot_id = data.get("bridge_boot_id")
            previous = self._registry.update_state(
                data["robot_id"], abstract_state=data["abstract_state"], sdk_state=data["sdk_state"],
                control_owner=data.get("control_owner"), active_run=data.get("active_run"),
                bridge_boot_id=boot_id,
            )
            if self._on_state is not None:
                self._on_state(data["robot_id"], previous, boot_id)
        elif message_kind == "ack" and self._on_ack is not None:
            from shared.schemas.commands import Command

            self._on_ack(Command(**data))

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
