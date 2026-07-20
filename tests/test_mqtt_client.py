import json

from realtime.mqtt_client import BackendMqttClient
from robots.registry import RobotRegistry


class _FakeMqttClient:
    def __init__(self):
        self.on_message = None
        self.on_connect = None
        self.subscriptions = []
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
