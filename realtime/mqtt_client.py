from __future__ import annotations

import json
import logging
from typing import Callable, Optional, Tuple

from shared.schemas.version import SCHEMA_VERSION

logger = logging.getLogger("backend.realtime.mqtt_client")

MqttClientFactory = Callable[[], object]


def real_mqtt_client_factory():
    import paho.mqtt.client as mqtt

    return mqtt.Client()


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
    ):
        self._broker_url = broker_url
        self._registry = registry
        self._on_ack = on_ack
        self._client = mqtt_client_factory()
        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect

    def connect(self) -> None:
        host, port = self._parse_broker_url(self._broker_url)
        self._client.connect(host, port)
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish_command(self, robot_id: str, command) -> None:
        record = self._registry.get(robot_id)
        site_id = record.site_id if record is not None else "unknown"
        topic = f"roverhub/{site_id}/{robot_id}/commands"
        self._client.publish(topic, command.model_dump_json(), qos=1)

    def _on_connect(self, client, userdata, flags, rc) -> None:
        client.subscribe("roverhub/admin/+/register", qos=1)
        client.subscribe("roverhub/+/+/telemetry", qos=0)
        client.subscribe("roverhub/+/+/state", qos=1)
        client.subscribe("roverhub/+/+/heartbeat", qos=0)
        client.subscribe("roverhub/+/+/ack", qos=1)

    def _on_message(self, client, userdata, message) -> None:
        self.handle_message(message.topic, message.payload)

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
            )
            return

        if len(parts) != 4 or parts[0] != "roverhub":
            return
        message_kind = parts[3]

        if message_kind in ("telemetry", "heartbeat"):
            self._registry.mark_seen(data["robot_id"])
        elif message_kind == "state":
            self._registry.update_state(
                data["robot_id"], abstract_state=data["abstract_state"], sdk_state=data["sdk_state"],
            )
        elif message_kind == "ack" and self._on_ack is not None:
            from shared.schemas.commands import Command

            self._on_ack(Command(**data))

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
            logger.error(
                "SCHEMA VERSION MISMATCH for robot %s: edge reports %s, cloud has %s. "
                "The two ends disagree on the wire contract; expect missing or malformed "
                "telemetry/command fields. Redeploy shared/ to the edge device and restart "
                "the bridge -- see roverhub-edge/scripts/check_edge_sync.sh",
                robot_id, reported, SCHEMA_VERSION,
            )

    @staticmethod
    def _parse_broker_url(url: str) -> Tuple[str, int]:
        without_scheme = url.split("://", 1)[-1]
        host, _, port = without_scheme.partition(":")
        return host, int(port) if port else 1883
