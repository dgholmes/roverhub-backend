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


# --- schema skew advice ----------------------------------------------------
#
# The remedy depends on which side is behind, and the original message always said
# "redeploy shared/ to the edge device" -- exactly backwards right after a schema
# bump reaches the devices while this backend still runs the version it imported
# at startup. That wording sent someone to fix the wrong machine.


def test_skew_advice_when_the_cloud_is_behind():
    advice = BackendMqttClient._skew_remedy("0.0.3", "0.0.2")

    assert "CLOUD is the older side" in advice
    assert "restart" in advice.lower()
    # Must not send the operator to the device, which is already current.
    assert "update_edge" not in advice


def test_skew_advice_when_the_edge_is_behind():
    advice = BackendMqttClient._skew_remedy("0.0.1", "0.0.2")

    assert "EDGE is the older side" in advice
    assert "update_edge.sh" in advice


def test_skew_advice_compares_numerically_not_lexically():
    # "0.0.10" < "0.0.9" as strings, which would invert the advice.
    assert "EDGE is the older side" in BackendMqttClient._skew_remedy("0.0.9", "0.0.10")
    assert "CLOUD is the older side" in BackendMqttClient._skew_remedy("0.0.10", "0.0.9")


def test_skew_advice_falls_back_when_a_version_is_unparseable():
    advice = BackendMqttClient._skew_remedy("weird", "0.0.2")

    assert "disagree on the wire contract" in advice
    assert "check_edge_sync.sh" in advice


def test_unparseable_version_does_not_raise():
    # This runs inside the MQTT callback; raising there killed the whole client.
    assert BackendMqttClient._parse_version(None) is None
    assert BackendMqttClient._parse_version("1.2.x") is None
    assert BackendMqttClient._parse_version("0.0.3") == (0, 0, 3)


def test_a_mismatched_registration_still_registers_the_robot():
    """Version skew must warn, not reject: refusing the registration would take a
    robot fully offline over what is usually an additive change."""
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    payload = json.dumps({
        "bridge_id": "bridge-pi-01", "site_id": "site-dev",
        "robot_id": "robot-dev-02", "robot_type": "quad", "schema_version": "9.9.9",
    }).encode()

    client.handle_message("roverhub/admin/bridge-pi-01/register", payload)

    assert registry.get("robot-dev-02") is not None


# --- Edge host info / metrics (schema 0.0.9) --------------------------------

HOST = {"hostname": "jetson", "os": "Ubuntu 22.04.4 LTS", "arch": "aarch64",
        "bridge_version": "6d949c8", "booted_at": "2026-10-15T00:00:00Z"}


def _register(client, **extra):
    client.handle_message("roverhub/admin/bridge-dev-01/register", json.dumps({
        "bridge_id": "bridge-dev-01", "site_id": "site-dev",
        "robot_id": "robot-dev-01", "robot_type": "quad", **extra,
    }).encode())


def test_registration_carries_host_info_bridge_id_and_schema_version():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)

    _register(client, schema_version="0.0.9", host=HOST)

    record = registry.get("robot-dev-01")
    assert record.bridge_id == "bridge-dev-01"
    assert record.schema_version == "0.0.9"
    assert record.host["hostname"] == "jetson"
    assert record.host["device_model"] is None  # filled to the full schema shape


def test_registration_from_an_old_bridge_has_no_host():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    _register(client)
    assert registry.get("robot-dev-01").host is None


def test_invalid_host_info_is_dropped_without_losing_the_registration():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    _register(client, host={"hostname": ["not", "a", "string"]})
    record = registry.get("robot-dev-01")
    assert record is not None and record.online is True
    assert record.host is None


def test_heartbeat_carries_host_metrics():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    _register(client)

    client.handle_message("roverhub/site-dev/robot-dev-01/heartbeat", json.dumps({
        "robot_id": "robot-dev-01", "timestamp": "2026-10-15T00:00:05Z",
        "host_metrics": {"cpu_percent": 12.5, "soc_temp_c": 51.0},
    }).encode())

    record = registry.get("robot-dev-01")
    assert record.host_metrics["cpu_percent"] == 12.5
    assert record.host_metrics["memory_percent"] is None
    assert record.host_metrics_at == "2026-10-15T00:00:05Z"


def _state_payload(**extra):
    return json.dumps({
        "robot_id": "robot-dev-01", "site_id": "site-dev",
        "abstract_state": "STAND", "sdk_state": "balance_stand",
        "updated_at": "2026-07-20T00:00:00Z", **extra,
    }).encode()


def test_state_message_with_new_fields_calls_on_state():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    calls = []
    client = BackendMqttClient(
        "mqtt://localhost:1883", registry, _FakeMqttClient,
        on_state=lambda robot_id, previous, boot: calls.append((robot_id, previous, boot)),
    )
    run = {"run_id": "r-1", "status": "running", "started_at": "2026-10-29T00:00:00Z"}

    client.handle_message("roverhub/site-dev/robot-dev-01/state", _state_payload(
        control_owner="mission:r-1", active_run=run, bridge_boot_id="boot-a"))
    client.handle_message("roverhub/site-dev/robot-dev-01/state", _state_payload(bridge_boot_id="boot-b"))

    assert calls == [("robot-dev-01", None, "boot-a"), ("robot-dev-01", "boot-a", "boot-b")]
    record = registry.get("robot-dev-01")
    assert record.bridge_boot_id == "boot-b"
    assert record.control_owner is None and record.active_run is None  # second message omitted them


def test_state_message_from_0_0_9_bridge_still_updates_state():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    calls = []
    client = BackendMqttClient(
        "mqtt://localhost:1883", registry, _FakeMqttClient,
        on_state=lambda robot_id, previous, boot: calls.append((robot_id, previous, boot)),
    )

    client.handle_message("roverhub/site-dev/robot-dev-01/state", _state_payload())

    assert registry.get("robot-dev-01").abstract_state == "STAND"
    assert calls == [("robot-dev-01", None, None)]


def test_state_message_stores_control_owner_and_active_run():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    run = {"run_id": "r-1", "status": "running", "started_at": "2026-10-29T00:00:00Z"}

    client.handle_message("roverhub/site-dev/robot-dev-01/state", _state_payload(
        control_owner="mission:r-1", active_run=run, bridge_boot_id="boot-a"))

    record = registry.get("robot-dev-01")
    assert record.control_owner == "mission:r-1"
    assert record.active_run == run


_NAV_GOAL = {"goal_id": "c-1", "x_m": 1.0, "y_m": 2.0, "yaw_rad": None, "map_id": "map-venue",
             "state": "aborted", "reason": "localization_lost", "updated_at": "2026-11-10T00:00:00Z"}


def test_state_message_stores_nav_goal():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)

    client.handle_message("roverhub/site-dev/robot-dev-01/state", _state_payload(nav_goal=_NAV_GOAL))

    assert registry.get("robot-dev-01").nav_goal == _NAV_GOAL


def test_state_message_without_nav_goal_gives_none():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    client.handle_message("roverhub/site-dev/robot-dev-01/state", _state_payload(nav_goal=_NAV_GOAL))

    client.handle_message("roverhub/site-dev/robot-dev-01/state", _state_payload())  # an old bridge

    assert registry.get("robot-dev-01").nav_goal is None


def test_state_message_with_a_malformed_nav_goal_still_updates_state():
    """Display data must never cost the state message that carried it."""
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)

    client.handle_message("roverhub/site-dev/robot-dev-01/state",
                          _state_payload(nav_goal={**_NAV_GOAL, "state": "teleporting"}))

    record = registry.get("robot-dev-01")
    assert record.abstract_state == "STAND"
    assert record.nav_goal is None


def test_publish_command_refuses_a_robot_the_registry_does_not_know():
    # It used to publish to roverhub/unknown/<robot>/commands -- a topic no
    # bridge subscribes to -- and report the command as sent.
    from realtime.mqtt_client import CommandPublishError, UnknownRobotError

    fake = _FakeMqttClient()
    client = BackendMqttClient("mqtt://localhost:1883", RobotRegistry(), lambda: fake)
    command = Command(command_id="cmd-1", robot_id="ghost", type="ESTOP", label="E-Stop", initiated_by="x")

    with pytest.raises(UnknownRobotError):
        client.publish_command("ghost", command)
    assert fake.published == []
    # A CommandPublishError, so every caller that already settles an unsent
    # command (marks it failed, fails its run) does so for this one too.
    assert issubclass(UnknownRobotError, CommandPublishError)


def test_publish_command_still_publishes_to_a_known_robot_that_is_offline():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    registry.mark_offline("robot-dev-01")
    fake = _FakeMqttClient()
    client = BackendMqttClient("mqtt://localhost:1883", registry, lambda: fake)
    command = Command(command_id="cmd-1", robot_id="robot-dev-01", type="ESTOP", label="E-Stop", initiated_by="x")

    client.publish_command("robot-dev-01", command)

    assert fake.published[-1][0] == "roverhub/site-dev/robot-dev-01/commands"


def test_registration_records_the_map_the_bridge_navigates_on():
    registry = RobotRegistry()
    client = BackendMqttClient("mqtt://localhost:1883", registry, _FakeMqttClient)
    base = {"robot_id": "rover-j", "site_id": "site-a", "robot_type": "quad", "schema_version": "0.0.16"}

    client.handle_message("roverhub/admin/rover-j/register", json.dumps({**base, "nav_map_id": "map-office"}).encode())
    assert registry.get("rover-j").nav_map_id == "map-office"

    # A bridge with nav off (or older than 0.0.16) says nothing: none, not the last value.
    client.handle_message("roverhub/admin/rover-j/register", json.dumps(base).encode())
    assert registry.get("rover-j").nav_map_id is None
