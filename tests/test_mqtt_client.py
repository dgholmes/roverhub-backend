import json

from realtime.mqtt_client import BackendMqttClient
from robots.registry import RobotRegistry
from shared.schemas.commands import Command


class _FakeMqttClient:
    def __init__(self):
        self.on_message = None
        self.on_connect = None
        self.subscriptions = []
        self.published = []
        self.connected_to = None

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
