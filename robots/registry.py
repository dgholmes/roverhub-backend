from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class RobotRecord:
    robot_id: str
    site_id: str
    robot_type: str
    name: str
    last_seen: Optional[datetime] = None
    online: bool = False


class RobotRegistry:
    """In-memory registry of known robots -- no database this round. State
    is lost on process restart; that's an accepted limitation, not a bug."""

    def __init__(self):
        self._robots: Dict[str, RobotRecord] = {}

    def upsert_from_registration(self, robot_id: str, site_id: str, robot_type: str) -> None:
        existing = self._robots.get(robot_id)
        name = existing.name if existing else robot_id
        self._robots[robot_id] = RobotRecord(
            robot_id=robot_id, site_id=site_id, robot_type=robot_type,
            name=name, last_seen=datetime.now(timezone.utc), online=True,
        )

    def mark_seen(self, robot_id: str) -> None:
        record = self._robots.get(robot_id)
        if record is not None:
            record.last_seen = datetime.now(timezone.utc)
            record.online = True

    def mark_offline(self, robot_id: str) -> None:
        record = self._robots.get(robot_id)
        if record is not None:
            record.online = False

    def get(self, robot_id: str) -> Optional[RobotRecord]:
        return self._robots.get(robot_id)

    def list_all(self) -> List[RobotRecord]:
        return list(self._robots.values())
