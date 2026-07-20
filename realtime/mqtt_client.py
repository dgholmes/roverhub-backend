from __future__ import annotations

import json
import logging
from typing import Callable, Tuple

logger = logging.getLogger("backend.realtime.mqtt_client")

MqttClientFactory = Callable[[], object]


def real_mqtt_client_factory():
    import paho.mqtt.client as mqtt

    return mqtt.Client()


class BackendMqttClient:
    """Subscribes to bridge registration, telemetry, state, and heartbeat
    topics, updating the in-memory RobotRegistry as messages arrive. No
    persistence this round."""

    def __init__(self, broker_url: str, registry, mqtt_client_factory: MqttClientFactory):
        self._broker_url = broker_url
        self._registry = registry
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

    def _on_connect(self, client, userdata, flags, rc) -> None:
        client.subscribe("roverhub/admin/+/register", qos=1)
        client.subscribe("roverhub/+/+/telemetry", qos=0)
        client.subscribe("roverhub/+/+/state", qos=1)
        client.subscribe("roverhub/+/+/heartbeat", qos=0)

    def _on_message(self, client, userdata, message) -> None:
        self.handle_message(message.topic, message.payload)

    def handle_message(self, topic: str, payload: bytes) -> None:
        """Testable core -- takes a raw topic string and payload bytes so
        tests never need to construct a real paho MQTTMessage."""
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            logger.warning("dropping unparseable MQTT payload on %s", topic)
            return

        parts = topic.split("/")
        if topic.startswith("roverhub/admin/") and topic.endswith("/register"):
            self._registry.upsert_from_registration(
                robot_id=data["robot_id"], site_id=data["site_id"], robot_type=data["robot_type"],
            )
            return

        if len(parts) != 4 or parts[0] != "roverhub":
            return
        message_kind = parts[3]

        if message_kind == "telemetry":
            self._registry.mark_seen(data["robot_id"])
        elif message_kind == "state":
            self._registry.update_state(
                data["robot_id"], abstract_state=data["abstract_state"], sdk_state=data["sdk_state"],
            )
        elif message_kind == "heartbeat":
            self._registry.mark_seen(data["robot_id"])

    @staticmethod
    def _parse_broker_url(url: str) -> Tuple[str, int]:
        without_scheme = url.split("://", 1)[-1]
        host, _, port = without_scheme.partition(":")
        return host, int(port) if port else 1883
