from __future__ import annotations

import unittest

from backend.hermes_daemon.daemon import (
    event_from_environment_alert,
    event_from_game_log,
    extract_json_object,
    fallback_rule_decision,
)


class HermesDaemonTests(unittest.TestCase):
    def test_event_from_game_log_uses_metadata(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hello",
                "metadata": {
                    "persona": "A Test",
                    "event_type": "player_chat",
                    "map_id": 42,
                    "map_name": "Ascalon City",
                    "session_id": "s1",
                },
            }
        )

        self.assertEqual(event.persona, "A Test")
        self.assertEqual(event.event_type, "player_chat")
        self.assertEqual(event.map_id, 42)
        self.assertEqual(event.map_name, "Ascalon City")

    def test_event_from_game_log_preserves_gameplay_metadata(self) -> None:
        event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "Party member down.",
                "payload": {
                    "persona": "A Test",
                    "event_type": "party_member_down",
                    "agent_id": 42,
                    "agent_name": "Mhenlo",
                    "severity": "HIGH",
                },
            }
        )

        self.assertEqual(event.event_type, "party_member_down")
        self.assertEqual(event.agent_id, 42)
        self.assertEqual(event.agent_name, "Mhenlo")
        self.assertEqual(event.severity, "HIGH")

    def test_extract_json_object_from_wrapped_text(self) -> None:
        parsed = extract_json_object('Decision: {"should_speak": false, "response": ""}')

        self.assertFalse(parsed["should_speak"])

    def test_fallback_rule_replies_to_party_chat(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "id": 123,
                    "sender": "Player",
                    "channel": "party",
                    "message": "hello",
                    "metadata": {"event_type": "player_chat"},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.channel_override, "CHANNEL_PARTY")

    def test_reply_can_include_trigger_log_id(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "id": 123,
                    "sender": "Player",
                    "channel": "party",
                    "message": "hello",
                    "metadata": {"event_type": "player_chat"},
                }
            )
        )

        reply = decision.to_reply("A Test", "session", trigger_log_id=123)

        self.assertIsNotNone(reply)
        self.assertEqual(reply.to_supabase_insert()["trigger_log_id"], 123)

    def test_environment_alert_row_becomes_event(self) -> None:
        event = event_from_environment_alert(
            {
                "id": 10,
                "alert_type": "danger_spike",
                "severity": "HIGH",
                "map_id": 148,
                "message": "3 hostile enemies are close.",
                "agent_id": 99,
                "distance": 800,
                "payload": {
                    "persona": "A Test",
                    "hostile_count": 4,
                    "close_hostile_count": 3,
                    "session_id": "local-playtest",
                },
            }
        )

        self.assertEqual(event.event_type, "environment_alert")
        self.assertEqual(event.alert_type, "danger_spike")
        self.assertEqual(event.close_hostile_count, 3)
        self.assertEqual(event.closest_hostile_distance, 800)

    def test_fallback_rule_replies_to_danger_spike(self) -> None:
        decision = fallback_rule_decision(
            event_from_environment_alert(
                {
                    "alert_type": "danger_spike",
                    "severity": "HIGH",
                    "message": "3 hostile enemies are close.",
                    "payload": {"close_hostile_count": 3},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "HIGH")

    def test_fallback_rule_replies_to_under_attack(self) -> None:
        decision = fallback_rule_decision(
            event_from_environment_alert(
                {
                    "alert_type": "under_attack",
                    "severity": "HIGH",
                    "message": "Player is under attack.",
                    "payload": {"player_hp": 0.42},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "HIGH")
        self.assertIn("42%", decision.response)

    def test_fallback_rule_replies_to_combat_started(self) -> None:
        decision = fallback_rule_decision(
            event_from_environment_alert(
                {
                    "alert_type": "combat_started",
                    "severity": "HIGH",
                    "message": "Combat started.",
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "HIGH")
        self.assertIn("Contact", decision.response)

    def test_fallback_rule_replies_to_party_member_down(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "Party member down.",
                    "payload": {
                        "event_type": "party_member_down",
                        "agent_id": 42,
                        "agent_name": "Mhenlo",
                    },
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "HIGH")
        self.assertIn("Mhenlo", decision.response)


if __name__ == "__main__":
    unittest.main()
