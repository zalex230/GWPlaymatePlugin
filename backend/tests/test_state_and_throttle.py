from __future__ import annotations

import unittest

from backend.shared.models import TelemetryEvent
from backend.shared.state import LiveWorldState
from backend.shared.throttle import EventThrottle


class StateAndThrottleTests(unittest.TestCase):
    def test_world_state_bounds_chat_history(self) -> None:
        state = LiveWorldState(recent_chat_limit=2)
        for index in range(3):
            state.apply_event(
                TelemetryEvent(
                    persona="A Test",
                    event_type="player_chat",
                    sender="Player",
                    channel="party",
                    message=f"line {index}",
                )
            )

        self.assertEqual(list(state.recent_chat_history), ["[Player]: line 1", "[Player]: line 2"])

    def test_snapshot_throttle_rejects_duplicate_immediate_snapshot(self) -> None:
        throttle = EventThrottle(snapshot_min_interval_seconds=60)
        event = TelemetryEvent(
            persona="A Test",
            event_type="snapshot",
            sender="System",
            channel="system",
            message="snapshot",
            map_id=1,
            active_quest_id=2,
        )

        self.assertTrue(throttle.should_accept(event))
        self.assertFalse(throttle.should_accept(event))


if __name__ == "__main__":
    unittest.main()
