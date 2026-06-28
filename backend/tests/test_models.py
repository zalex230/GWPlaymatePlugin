from __future__ import annotations

import unittest

from pydantic import ValidationError

from backend.shared.models import CompanionReplyRow, HermesDecision, TelemetryEvent


class ModelTests(unittest.TestCase):
    def test_telemetry_normalizes_channel_and_event_type(self) -> None:
        event = TelemetryEvent(
            persona="A Test",
            event_type=" Player_Chat ",
            sender="Player",
            channel=" Party ",
            message="hello",
            map_name="Ascalon City",
        )

        self.assertEqual(event.event_type, "player_chat")
        self.assertEqual(event.channel, "party")
        self.assertEqual(event.to_game_log_insert()["payload"]["persona"], "A Test")
        self.assertEqual(event.to_game_log_insert()["payload"]["map_name"], "Ascalon City")

    def test_telemetry_requires_message(self) -> None:
        with self.assertRaises(ValidationError):
            TelemetryEvent(event_type="player_chat", sender="Player", channel="party", message="")

    def test_hermes_decision_to_reply(self) -> None:
        decision = HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="HIGH",
            response="Hold up.",
        )

        reply = decision.to_reply("A Test", "session")

        self.assertIsNotNone(reply)
        self.assertEqual(reply.channel, "party")
        self.assertEqual(reply.urgency, "HIGH")

    def test_reply_insert_includes_trigger_log_id(self) -> None:
        decision = HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            response="Hold up.",
        )

        row = decision.to_reply("A Test", "session", trigger_log_id=7).to_supabase_insert()

        self.assertEqual(row["trigger_log_id"], 7)
        self.assertEqual(row["payload"]["trigger_log_id"], 7)

    def test_reply_row_maps_audio_payload_to_reply_item(self) -> None:
        row = CompanionReplyRow(
            id=1,
            persona="A Test",
            message="On it.",
            payload={
                "audio_url": "https://example.supabase.co/storage/v1/object/sign/playmate-tts/test.mp3",
                "audio_mime_type": "audio/mpeg",
                "audio_expires_at": "2026-06-27T12:00:00+00:00",
                "multi_message": True,
                "line_index": 2,
                "line_count": 2,
            },
        )

        item = row.to_reply_item()

        self.assertEqual(item.message, "On it.")
        self.assertEqual(item.audio_mime_type, "audio/mpeg")
        self.assertTrue(item.audio_url.startswith("https://example.supabase.co/"))
        self.assertTrue(item.multi_message)
        self.assertEqual(item.line_index, 2)
        self.assertEqual(item.line_count, 2)

    def test_reply_row_uses_empty_audio_fields_for_text_only_reply(self) -> None:
        row = CompanionReplyRow(
            id=2,
            persona="A Test",
            message="Text only.",
            payload={},
        )

        item = row.to_reply_item()

        self.assertEqual(item.message, "Text only.")
        self.assertEqual(item.audio_url, "")
        self.assertEqual(item.audio_mime_type, "")
        self.assertEqual(item.audio_expires_at, "")

    def test_environment_alert_insert_maps_radar_fields(self) -> None:
        event = TelemetryEvent(
            persona="A Test",
            event_type="environment_alert",
            sender="System",
            channel="system",
            message="Enemy nearby.",
            alert_type="enemy_patrol_nearby",
            severity="HIGH",
            map_id=148,
            player_x=10,
            player_y=20,
            hostile_count=3,
            close_hostile_count=2,
            closest_hostile_agent_id=99,
            closest_hostile_distance=1234.5,
        )

        row = event.to_environment_alert_insert()

        self.assertEqual(row["alert_type"], "enemy_patrol_nearby")
        self.assertEqual(row["severity"], "HIGH")
        self.assertEqual(row["agent_id"], 99)
        self.assertEqual(row["distance"], 1234.5)
        self.assertEqual(row["payload"]["close_hostile_count"], 2)

    def test_gameplay_event_metadata_preserves_context_fields(self) -> None:
        event = TelemetryEvent(
            persona="A Test",
            event_type="party_member_down",
            sender="System",
            channel="system",
            message="Party member down.",
            agent_id=42,
            agent_name="Mhenlo",
            objective_id=8,
            objective_name="Keep Rurik alive",
            progress_current=3,
            progress_total=10,
            foes_killed=3,
            foes_remaining=7,
            severity="HIGH",
        )

        payload = event.to_game_log_insert()["payload"]

        self.assertEqual(payload["agent_id"], 42)
        self.assertEqual(payload["agent_name"], "Mhenlo")
        self.assertEqual(payload["objective_id"], 8)
        self.assertEqual(payload["objective_name"], "Keep Rurik alive")
        self.assertEqual(payload["progress_current"], 3)
        self.assertEqual(payload["progress_total"], 10)
        self.assertEqual(payload["foes_remaining"], 7)

    def test_memory_insert_maps_character_context(self) -> None:
        from backend.shared.models import MemoryInsert

        memory = MemoryInsert(
            character_name="A Test",
            session_id="session-1",
            memory_type="mission_summary",
            title="Ruins of Surmia",
            summary_text="Cleared a mission and found a rare sword.",
            map_id=148,
            active_quest_id=1185,
            rare_items=[{"name": "Rare Sword", "rarity": "gold"}],
            tags=["mission", "rare_item"],
            source_log_start_id=1,
            source_log_end_id=9,
        )

        row = memory.to_supabase_insert()

        self.assertEqual(row["character_name"], "A Test")
        self.assertEqual(row["memory_type"], "mission_summary")
        self.assertEqual(row["rare_items"][0]["name"], "Rare Sword")
        self.assertEqual(row["tags"], ["mission", "rare_item"])


if __name__ == "__main__":
    unittest.main()
