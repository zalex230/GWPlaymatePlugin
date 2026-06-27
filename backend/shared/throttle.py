from __future__ import annotations

import time
from dataclasses import dataclass, field

from backend.shared.constants import SNAPSHOT_EVENT_TYPES
from backend.shared.models import TelemetryEvent


@dataclass
class EventThrottle:
    snapshot_min_interval_seconds: float = 8.0
    _last_snapshot_by_key: dict[tuple[str, str, int, int], float] = field(default_factory=dict)

    def should_accept(self, event: TelemetryEvent) -> bool:
        if event.event_type not in SNAPSHOT_EVENT_TYPES:
            return True

        key = (event.persona, event.event_type, event.map_id, event.active_quest_id)
        now = time.monotonic()
        last_seen = self._last_snapshot_by_key.get(key)
        if last_seen is not None and now - last_seen < self.snapshot_min_interval_seconds:
            return False
        self._last_snapshot_by_key[key] = now
        return True
