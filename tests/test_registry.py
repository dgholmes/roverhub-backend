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
