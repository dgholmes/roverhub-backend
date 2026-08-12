import json

import pytest

from realtime.mqtt_client import BackendMqttClient, CommandPublishError
from robots.registry import RobotRegistry
from shared.schemas.commands import Command


class _FakeMessageInfo:
    def __init__(self, rc):
        self.rc = rc


class _FakeMqttClient:
    def __init__(self):
        self.on_message = None
        self.on_connect = None
        self.subscriptions = []
        self.published = []
        self.connected_to = None
        self.on_disconnect = None
        self.reconnect_delay = None
        # paho returns MQTT_ERR_SUCCESS (0) from publish(); override to simulate
        # a publish that the client could not hand to the broker.
        self.publish_rc = 0

    def connect(self, host, port):
        self.connected_to = (host, port)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        return _FakeMessageInfo(self.publish_rc)

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect_delay = (min_delay, max_delay)


def test_handle_message_registers_robot_from_admin_topic():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    payload = json.dumps({
        "bridge_id": "bridge-dev-01", "site_id": "site-dev",
        "robot_id": "robot-dev-01", "robot_type": "wheel",
    }).encode()

    client.handle_message("roverhub/admin/bridge-dev-01/register", payload)

    record = registry.get("robot-dev-01")
    assert record.robot_type == "wheel"
    assert record.online is True


def test_handle_message_marks_seen_on_telemetry():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    registry.mark_offline("robot-dev-01")
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    payload = json.dumps({"robot_id": "robot-dev-01", "site_id": "site-dev"}).encode()

    client.handle_message("roverhub/site-dev/robot-dev-01/telemetry", payload)

    assert registry.get("robot-dev-01").online is True


def test_handle_message_ignores_unparseable_payload():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    client.handle_message("roverhub/site-dev/robot-dev-01/telemetry", b"not json")
    assert registry.list_all() == []


def test_on_connect_subscribes_to_register_and_telemetry():
    registry = RobotRegistry()
    fake = _FakeMqttClient()
    client = BackendMqttClient("mqtt://localhost:1883", registry, lambda: fake)
    client._on_connect(fake, None, None, 0)
    assert ("roverhub/admin/+/register", 1) in fake.subscriptions
    assert ("roverhub/+/+/telemetry", 0) in fake.subscriptions


def test_handle_message_updates_state_on_state_topic():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    payload = json.dumps({
        "robot_id": "robot-dev-01", "site_id": "site-dev",
        "abstract_state": "STAND", "sdk_state": "balance_stand",
        "updated_at": "2026-07-20T00:00:00Z",
    }).encode()

    client.handle_message("roverhub/site-dev/robot-dev-01/state", payload)

    record = registry.get("robot-dev-01")
    assert record.abstract_state == "STAND"


def test_handle_message_marks_seen_on_heartbeat_topic():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    registry.mark_offline("robot-dev-01")
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    payload = json.dumps({
        "bridge_id": "bridge-dev-01", "robot_id": "robot-dev-01", "site_id": "site-dev",
        "timestamp": "2026-07-20T00:00:00Z", "sdk_connected": True, "dds_connected": False,
        "cloud_connected": True, "battery_pct": 80.0, "mission_active": False,
    }).encode()

    client.handle_message("roverhub/site-dev/robot-dev-01/heartbeat", payload)

    assert registry.get("robot-dev-01").online is True


def test_on_connect_subscribes_to_state_and_heartbeat():
    registry = RobotRegistry()
    fake = _FakeMqttClient()
    client = BackendMqttClient("mqtt://localhost:1883", registry, lambda: fake)
    client._on_connect(fake, None, None, 0)
    assert ("roverhub/+/+/state", 1) in fake.subscriptions
    assert ("roverhub/+/+/heartbeat", 0) in fake.subscriptions


def test_publish_command_uses_registry_site_id():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    fake = _FakeMqttClient()
    client = BackendMqttClient("mqtt://localhost:1883", registry, lambda: fake)
    command = Command(command_id="cmd-1", robot_id="robot-dev-01", type="ESTOP", label="E-Stop", initiated_by="operator-1")

    client.publish_command("robot-dev-01", command)

    topic, payload, qos = fake.published[-1]
    assert topic == "roverhub/site-dev/robot-dev-01/commands"
    assert qos == 1


def test_handle_message_applies_ack_via_callback():
    registry = RobotRegistry()
    acks = []
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient, on_ack=acks.append)
    payload = json.dumps({
        "command_id": "cmd-1", "robot_id": "robot-dev-01", "type": "ESTOP",
        "label": "E-Stop", "current_stage": "execution_completed",
        "stages": [], "initiated_by": "operator-1",
    }).encode()

    client.handle_message("roverhub/site-dev/robot-dev-01/ack", payload)

    assert len(acks) == 1
    assert acks[0].current_stage == "execution_completed"


def test_on_connect_subscribes_to_ack():
    registry = RobotRegistry()
    fake = _FakeMqttClient()
    client = BackendMqttClient("mqtt://localhost:1883", registry, lambda: fake)
    client._on_connect(fake, None, None, 0)
    assert ("roverhub/+/+/ack", 1) in fake.subscriptions


def _message(topic, payload):
    """Minimal stand-in for paho's MQTTMessage, which is what paho hands the
    on_message callback."""

    class _M:
        pass

    m = _M()
    m.topic = topic
    m.payload = payload
    return m


# --- The paho callback boundary must never raise -----------------------------
#
# paho 1.6 re-raises whatever escapes on_message, and its network thread dies
# with the exception -- taking message ingestion AND command publishing down
# permanently, with no reconnect and no error anywhere. Observed in production
# 2026-08-11: the backend went silent at 23:55:42, mosquitto dropped it 90s
# later for "exceeded timeout", and every command afterwards sat at "Sending..."
# forever while the REST API kept happily returning 200.


def test_on_message_does_not_propagate_exceptions_from_bad_ack():
    registry = RobotRegistry()
    client = BackendMqttClient(
        "mqtt://localhost:1883", registry, _FakeMqttClient, on_ack=lambda c: None,
    )
    # Fails Command validation -- the real trigger shape.
    payload = json.dumps({"not": "a command"}).encode()

    client._on_message(None, None, _message("roverhub/site-dev/robot-dev-01/ack", payload))


def test_on_message_does_not_propagate_missing_field_errors():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)

    for topic in (
        "roverhub/site-dev/robot-dev-01/telemetry",
        "roverhub/site-dev/robot-dev-01/heartbeat",
        "roverhub/site-dev/robot-dev-01/state",
        "roverhub/admin/bridge-dev-01/register",
    ):
        client._on_message(None, None, _message(topic, json.dumps({}).encode()))


def test_one_poisonous_message_does_not_stop_later_good_ones():
    """The property that actually matters: ingestion survives a bad payload."""
    registry = RobotRegistry()
    registry.upsert_from_registration(
        robot_id="robot-dev-01", site_id="site-dev", robot_type="wheel",
    )
    client = BackendMqttClient(
        "mqtt://localhost:1883", registry, _FakeMqttClient, on_ack=lambda c: None,
    )

    client._on_message(None, None, _message(
        "roverhub/site-dev/robot-dev-01/ack", json.dumps({"bad": True}).encode()))
    client._on_message(None, None, _message(
        "roverhub/site-dev/robot-dev-01/state",
        json.dumps({"robot_id": "robot-dev-01", "abstract_state": "STAND",
                    "sdk_state": "balance_stand"}).encode()))

    assert registry.get("robot-dev-01").abstract_state == "STAND"


# --- A command that cannot be published must not look accepted ---------------


def test_publish_command_raises_when_the_client_cannot_publish():
    registry = RobotRegistry()
    registry.upsert_from_registration(
        robot_id="robot-dev-01", site_id="site-dev", robot_type="wheel",
    )
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    client._client.publish_rc = 4  # MQTT_ERR_NO_CONN

    command = Command(
        command_id="c1", robot_id="robot-dev-01", type="ESTOP", label="E-Stop",
        initiated_by="operator-dev",
    )

    with pytest.raises(CommandPublishError):
        client.publish_command("robot-dev-01", command)


def test_publish_command_succeeds_on_rc_zero():
    registry = RobotRegistry()
    registry.upsert_from_registration(
        robot_id="robot-dev-01", site_id="site-dev", robot_type="wheel",
    )
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    command = Command(
        command_id="c1", robot_id="robot-dev-01", type="ESTOP", label="E-Stop",
        initiated_by="operator-dev",
    )

    client.publish_command("robot-dev-01", command)

    assert len(client._client.published) == 1


# --- Recovery ----------------------------------------------------------------


def test_connect_registers_disconnect_handler_and_reconnect_backoff():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)

    client.connect()

    assert client._client.on_disconnect is not None
    assert client._client.reconnect_delay is not None


def test_on_disconnect_does_not_raise_on_unexpected_rc():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)

    client._on_disconnect(None, None, 7)
