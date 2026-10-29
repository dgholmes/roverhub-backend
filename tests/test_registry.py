from robots.registry import RobotRegistry


def test_upsert_from_registration_creates_online_record():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    record = registry.get("robot-dev-01")
    assert record.robot_type == "quad"
    assert record.online is True


def test_mark_offline_flips_online_flag():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    registry.mark_offline("robot-dev-01")
    assert registry.get("robot-dev-01").online is False


def test_list_all_returns_every_registered_robot():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-a", "site-dev", "quad")
    registry.upsert_from_registration("robot-b", "site-dev", "wheel")
    ids = {r.robot_id for r in registry.list_all()}
    assert ids == {"robot-a", "robot-b"}


def test_update_state_sets_abstract_and_sdk_state():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    registry.update_state("robot-dev-01", abstract_state="STAND", sdk_state="balance_stand")
    record = registry.get("robot-dev-01")
    assert record.abstract_state == "STAND"
    assert record.sdk_state == "balance_stand"


def test_update_state_on_unknown_robot_is_a_no_op():
    registry = RobotRegistry()
    registry.update_state("robot-ghost", abstract_state="STAND", sdk_state="balance_stand")
    assert registry.get("robot-ghost") is None


# --- Edge host info and acceptance (2026-10-15) -----------------------------

import pytest  # noqa: E402


def test_reregistration_keeps_state_name_metrics_and_decisions():
    """Registration repeats every heartbeat; it must not wipe what other topics
    or the operator own."""
    registry = RobotRegistry(acceptance_mode="manual")
    registry.upsert_from_registration("r", "site", "quad", bridge_id="b")
    registry.update_state("r", abstract_state="STAND", sdk_state="balance_stand")
    registry.update_host_metrics("r", {"cpu_percent": 5.0}, at="t1")
    registry.set_acceptance("r", "accepted")
    first_seen = registry.get("r").first_seen

    registry.upsert_from_registration("r", "site", "quad", bridge_id="b",
                                      schema_version="0.0.9", host={"hostname": "jetson"})

    record = registry.get("r")
    assert record.abstract_state == "STAND"
    assert record.host_metrics == {"cpu_percent": 5.0}
    assert record.acceptance == "accepted"
    assert record.acceptance_source == "operator"
    assert record.first_seen == first_seen
    assert record.host == {"hostname": "jetson"}
    assert record.schema_version == "0.0.9"


def test_auto_mode_accepts_every_new_robot():
    registry = RobotRegistry()
    registry.upsert_from_registration("r", "site", "quad")
    record = registry.get("r")
    assert (record.acceptance, record.acceptance_source) == ("accepted", "auto")


def test_manual_mode_makes_unknown_robots_pending():
    registry = RobotRegistry(acceptance_mode="manual")
    registry.upsert_from_registration("r", "site", "quad")
    record = registry.get("r")
    assert (record.acceptance, record.acceptance_source) == ("pending", None)


@pytest.mark.parametrize("listed", ["r", "bridge-r"])
def test_config_preaccepts_by_robot_or_bridge_id(listed):
    registry = RobotRegistry(acceptance_mode="manual", preaccepted=[f" {listed} ", ""])
    registry.upsert_from_registration("r", "site", "quad", bridge_id="bridge-r")
    record = registry.get("r")
    assert (record.acceptance, record.acceptance_source) == ("accepted", "config")


def test_forget_then_reregister_starts_over():
    registry = RobotRegistry(acceptance_mode="manual")
    registry.upsert_from_registration("r", "site", "quad")
    registry.set_acceptance("r", "rejected")

    assert registry.forget("r") is True
    assert registry.forget("r") is False
    registry.upsert_from_registration("r", "site", "quad")

    assert registry.get("r").acceptance == "pending"


def test_a_heartbeat_without_metrics_clears_stale_ones():
    registry = RobotRegistry()
    registry.upsert_from_registration("r", "site", "quad")
    registry.update_host_metrics("r", {"cpu_percent": 5.0}, at="t1")
    registry.update_host_metrics("r", None, at="t2")
    record = registry.get("r")
    assert record.host_metrics is None
    assert record.host_metrics_at is None


def test_an_unknown_acceptance_mode_is_refused():
    with pytest.raises(ValueError):
        RobotRegistry(acceptance_mode="strict")


# --- Run ownership and bridge boot id (edge mission runs, Task 10) -----------


def test_update_state_stores_owner_active_run_boot_and_returns_previous_boot():
    registry = RobotRegistry()
    registry.upsert_from_registration("robot-dev-01", "site-dev", "quad")
    active = {"run_id": "r-1", "status": "running", "started_at": "2026-10-29T00:00:00Z"}

    first = registry.update_state(
        "robot-dev-01", "STAND", "balance_stand",
        control_owner="mission:r-1", active_run=active, bridge_boot_id="boot-a",
    )
    stored = registry.get("robot-dev-01")
    assert (stored.control_owner, stored.active_run) == ("mission:r-1", active)
    second = registry.update_state("robot-dev-01", "STAND", "balance_stand", bridge_boot_id="boot-b")

    assert first is None
    assert second == "boot-a"
    record = registry.get("robot-dev-01")
    assert record.bridge_boot_id == "boot-b"
    # A state message without the new fields clears them rather than keeping stale ones.
    assert record.control_owner is None
    assert record.active_run is None


def test_update_state_on_unknown_robot_returns_none_for_boot():
    assert RobotRegistry().update_state("ghost", "STAND", "x", bridge_boot_id="b") is None
