from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ["GWPLAYMATE_DISABLE_MEMORY_WRITES"] = "1"

import backend.hermes.daemon as hermes_daemon
from backend.shared.models import CompanionReplyInsert, TelemetryEvent
from backend.hermes_daemon.daemon import (
    FILLER_ONLY_REPLY_PATTERN,
    LOW_QUALITY_REPLY_PATTERNS,
    AMBIENT_HEARTBEAT_ACTIVITY_SECONDS,
    AMBIENT_QUIP_MIN_SECONDS,
    ambient_quip,
    ambient_identity,
    ambient_heartbeat_reply,
    build_character_reply_prompt,
    clean_model_reply,
    event_from_environment_alert,
    event_from_game_log,
    expire_pending_replies_before_player_chat,
    extract_json_object,
    fallback_rule_decision,
    generate_tts_audio,
    gw_wiki_cache,
    gw_wiki_search_query,
    is_stale_polled_record,
    last_map_comment_by_session,
    map_comment_variant_by_session,
    known_persona_name,
    likely_gw_wiki_question,
    memory_event_from,
    memory_buffers,
    memory_last_write_at,
    misses_clear_player_intent,
    model_reply_has_bad_shape,
    persona_living_notes,
    prompt_relevant_memories,
    process_event,
    recent_conversation_context,
    recent_companion_context,
    recent_reply_texts,
    repair_model_reply,
    reply_expression,
    sanitize_local_persona_notes_for_prompt,
    sanitize_memory_for_prompt,
    should_flush_memory_buffer,
    should_use_direct_character_reply,
    should_use_fast_fallback_before_ollama,
    should_use_ollama_for_event,
    split_spoken_expression_label,
    validate_model_reply,
    world_state,
)
from backend.hermes.gw1_knowledge import resolve_gw1_context
from backend.hermes_eval.run_eval import run_eval


class HermesDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_insert_memory = hermes_daemon.insert_memory
        hermes_daemon.insert_memory = lambda memory: None
        recent_reply_texts.clear()
        hermes_daemon.recent_reply_texts_by_context.clear()
        hermes_daemon.recent_chat_history_by_context.clear()
        gw_wiki_cache.clear()
        last_map_comment_by_session.clear()
        map_comment_variant_by_session.clear()
        hermes_daemon.ambient_quip_variant_by_session.clear()
        memory_buffers.clear()
        memory_last_write_at.clear()
        world_state.last_spoken_at = 0
        world_state.last_interaction_timestamp = 0
        world_state.last_player_chat_at = 0
        world_state.persona = "Unknown Character"
        world_state.session_id = "local-playtest"
        world_state.map_id = 0
        world_state.map_name = ""
        world_state.close_hostile_count = 0
        world_state.recent_chat_history.clear()
        world_state.recent_alerts.clear()

    def tearDown(self) -> None:
        hermes_daemon.insert_memory = self._original_insert_memory
        memory_buffers.clear()
        memory_last_write_at.clear()

    def test_poll_watermarks_round_trip_to_configured_file(self) -> None:
        original_settings = hermes_daemon.settings
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "poll.json"
            hermes_daemon.settings = replace(original_settings, hermes_poll_state_path=str(state_path))
            try:
                hermes_daemon.save_poll_watermarks({"game_logs": 12, "environment_alerts": 34})

                self.assertEqual(hermes_daemon.load_poll_watermarks(), {"game_logs": 12, "environment_alerts": 34})
            finally:
                hermes_daemon.settings = original_settings

    def test_health_reports_realtime_connection_budget(self) -> None:
        original_settings = hermes_daemon.settings
        hermes_daemon.settings = replace(
            original_settings,
            hermes_enable_realtime=True,
            hermes_realtime_connection_budget=150,
        )
        try:
            payload = hermes_daemon.health()
        finally:
            hermes_daemon.settings = original_settings

        self.assertTrue(payload["realtime_enabled"])
        self.assertEqual(payload["realtime_planned_connections"], 1)
        self.assertEqual(payload["realtime_connection_budget"], 150)
        self.assertTrue(payload["realtime_budget_ok"])

    def test_known_persona_name_uses_configured_player_routes(self) -> None:
        original_settings = hermes_daemon.settings
        hermes_daemon.settings = replace(original_settings, hermes_persona_routes="Azwar=Meliora Andru")
        try:
            self.assertEqual(known_persona_name("Azwar"), "Meliora Andru")
            event = TelemetryEvent(
                persona="Azwar",
                event_type="player_chat",
                sender="Player",
                channel="party",
                message="where did you get your name?",
            )
            prompt = build_character_reply_prompt(event)
        finally:
            hermes_daemon.settings = original_settings

        self.assertIn("Meliora Andru", prompt)
        self.assertNotIn("Return only Azwar", prompt)

    def test_process_event_records_replies_under_routed_persona(self) -> None:
        original_settings = hermes_daemon.settings
        hermes_daemon.settings = replace(original_settings, hermes_persona_routes="Azwar=Meliora Andru")
        try:
            replies = process_event(
                TelemetryEvent(
                    persona="Azwar",
                    event_type="player_chat",
                    sender="Player",
                    channel="party",
                    message="hello",
                    session_id="local-playtest",
                ),
                use_ollama=False,
            )
        finally:
            hermes_daemon.settings = original_settings

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].persona, "Meliora Andru")

    def test_clean_model_reply_strips_instruction_echo_and_think_tags(self) -> None:
        raw = (
            "Do not include any meta commentary or explanations.\n\n"
            "<think>\n\n</think>\n\n"
            "Yeah, right there. Just need a moment to clear my head before we move again."
        )

        self.assertEqual(
            clean_model_reply(raw),
            "Yeah, right there. Just need a moment to clear my head before we move again.",
        )

    def test_instruction_echo_fragments_are_not_salvaged_as_replies(self) -> None:
        event = TelemetryEvent(
            persona="Azwar",
            event_type="player_chat",
            sender="Player",
            channel="party",
            message="hey Az",
            map_id=160,
        )

        for reply in (
            "Do",
            "Return",
            "Do not include any system text or meta commentary.",
            "Do not include any meta text like \"Here is your response:",
            "One or two natural party-chat lines, under 120 characters each.",
            "Thinking Process: 1. Analyze the Request: Persona: Azwar.",
            "Return in JSON format as a list of strings containing one line for chat and one short combat/ambient",
            "line under 119 characters each.",
        ):
            with self.subTest(reply=reply):
                with self.assertRaises(ValueError):
                    validate_model_reply(reply, event)
                self.assertIsNone(hermes_daemon.salvage_direct_player_chat_reply(reply, event))

    def test_azwar_ambient_quips_use_warrior_voice(self) -> None:
        event = TelemetryEvent(
            persona="Azwar",
            event_type="snapshot",
            sender="System",
            channel="system",
            message="quiet ambient moment",
            map_id=166,
            map_name="Green Hills County",
        )

        quip = ambient_quip(event)

        self.assertRegex(quip.lower(), r"view|footing|ridges|mud|trouble|slope")
        self.assertNotRegex(quip.lower(), r"boots|hair|pretty fields")

    def test_local_context_is_isolated_by_persona(self) -> None:
        original_settings = hermes_daemon.settings
        hermes_daemon.settings = replace(original_settings, supabase_url="", supabase_service_key="")
        try:
            azele_event = TelemetryEvent(
                source="test",
                persona="Azele",
                event_type="player_chat",
                sender="Player",
                channel="party",
                message="remember our charr plan",
                session_id="local-playtest",
            )
            meliora_event = TelemetryEvent(
                source="test",
                persona="Meliora Andru",
                event_type="player_chat",
                sender="Player",
                channel="party",
                message="hey Mel, ready to scout?",
                session_id="local-playtest",
            )
            hermes_daemon.record_recent_chat_event(azele_event)
            hermes_daemon.record_recent_reply("Azele", "local-playtest", "Yes. Charr first, then we breathe.")
            hermes_daemon.record_recent_chat_event(meliora_event)
            hermes_daemon.record_recent_reply("Meliora Andru", "local-playtest", "I am checking the trail.")

            prompt = build_character_reply_prompt(meliora_event)
        finally:
            hermes_daemon.settings = original_settings

        self.assertIn("hey Mel, ready to scout?", prompt)
        self.assertIn("I am checking the trail.", prompt)
        self.assertNotIn("remember our charr plan", prompt)
        self.assertNotIn("Charr first, then we breathe", prompt)

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

    def test_reply_expression_classifies_common_azele_moods(self) -> None:
        self.assertEqual(reply_expression("If Charr show, we do not hesitate."), "angry")
        self.assertEqual(reply_expression("Someone's down. Move, I can cover.", "HIGH"), "worried")
        self.assertEqual(reply_expression("Hold the line until help arrives from Ascalon City."), "angry")
        self.assertEqual(reply_expression("I know. Still nice to hear."), "confident")
        self.assertEqual(reply_expression("That was a little funny. A little."), "teasing")
        self.assertEqual(reply_expression("Seriously? Try not to make me say it twice."), "annoyed")
        self.assertEqual(reply_expression("You think I look pretty like this?"), "flirty")
        self.assertEqual(reply_expression("I’m glad you asked."), "happy")
        self.assertEqual(reply_expression("I’m here. What are we doing?"), "neutral")

    def test_spoken_expression_label_is_split_from_visible_text(self) -> None:
        cases = {
            "neutral": "neutral",
            "happy": "happy",
            "teasing": "teasing",
            "flirty": "flirty",
            "confident": "confident",
            "annoyed": "annoyed",
            "angry": "angry",
            "worried": "worried",
            "sad": "sad",
            "embarrassed": "embarrassed",
            "irritated": "annoyed",
            "scared": "worried",
            "playful": "teasing",
            "romantic": "flirty",
            "shy": "embarrassed",
        }

        for label, expected_expression in cases.items():
            with self.subTest(label=label):
                expression, message = split_spoken_expression_label(f"{label}: I know. Still nice to hear.")

                self.assertEqual(expression, expected_expression)
                self.assertEqual(message, "I know. Still nice to hear.")
        expression, message = split_spoken_expression_label("worried, Someone's down. Move, I can cover.")
        self.assertEqual(expression, "worried")
        self.assertEqual(message, "Someone's down. Move, I can cover.")
        expression, message = split_spoken_expression_label("confident - I know. Still nice to hear.")
        self.assertEqual(expression, "confident")
        self.assertEqual(message, "I know. Still nice to hear.")
        expression, message = split_spoken_expression_label("sad. I know. Give me a second.")
        self.assertEqual(expression, "sad")
        self.assertEqual(message, "I know. Give me a second.")

    def test_chatterbox_tts_payload_includes_expression_controls(self) -> None:
        original_settings = hermes_daemon.settings
        try:
            hermes_daemon.settings = replace(
                original_settings,
                chatterbox_tts_voice_sample="/tmp/azele.wav",
                chatterbox_tts_format="wav",
                chatterbox_tts_exaggeration=0.9,
                chatterbox_tts_temperature=0.6,
            )

            payload = hermes_daemon._chatterbox_tts_payload("That was funny.", expression="teasing")

            self.assertEqual(payload["input"], "That was funny.")
            self.assertEqual(payload["voice_sample_path"], "/tmp/azele.wav")
            self.assertEqual(payload["response_format"], "wav")
            self.assertEqual(payload["expression"], "teasing")
            self.assertEqual(payload["exaggeration"], 0.45)
            self.assertEqual(payload["temperature"], 0.9)
            self.assertNotIn("paralinguistic_tags", payload)
        finally:
            hermes_daemon.settings = original_settings

    def test_tts_payload_uses_pronunciation_text_only_for_audio(self) -> None:
        text = "Hmph, Azele says Ascalon City is home, not Old Ascalon."

        kokoro = hermes_daemon._kokoro_tts_payload(text)
        chatterbox = hermes_daemon._chatterbox_tts_payload(text, expression="happy")

        self.assertEqual(kokoro["input"], "Humph, Azelle says Ask-alon City is home, not Old Ask-alon.")
        self.assertEqual(chatterbox["input"], "Humph, Azelle says Ask-alon City is home, not Old Ask-alon.")
        self.assertEqual(text, "Hmph, Azele says Ascalon City is home, not Old Ascalon.")

    def test_kokoro_tts_voice_routes_by_persona(self) -> None:
        original_settings = hermes_daemon.settings
        try:
            hermes_daemon.settings = replace(original_settings, kokoro_tts_voice="af_default")

            self.assertEqual(hermes_daemon._kokoro_tts_payload("hi", persona="Azele")["voice"], "af_heart")
            self.assertEqual(hermes_daemon._kokoro_tts_payload("hi", persona="Meliora Andru")["voice"], "af_bella")
            self.assertEqual(hermes_daemon._kokoro_tts_payload("hi", persona="Other Character")["voice"], "af_default")
        finally:
            hermes_daemon.settings = original_settings

    def test_chatterbox_tts_payload_uses_distinct_mood_profiles(self) -> None:
        angry = hermes_daemon._chatterbox_tts_payload("Charr at the gate.", expression="angry")
        flirty = hermes_daemon._chatterbox_tts_payload("You noticed?", expression="flirty")
        worried = hermes_daemon._chatterbox_tts_payload("Someone is down.", expression="worried")

        self.assertEqual(angry["expression"], "angry")
        self.assertGreater(angry["temperature"], hermes_daemon._chatterbox_tts_payload("Fine.", expression="sad")["temperature"])
        self.assertEqual(flirty["expression"], "flirty")
        self.assertEqual(worried["expression"], "worried")
        self.assertNotIn("paralinguistic_tags", angry)
        self.assertNotIn("paralinguistic_tags", flirty)
        self.assertNotIn("paralinguistic_tags", worried)

    def test_chatterbox_tts_provider_falls_back_to_kokoro(self) -> None:
        original_settings = hermes_daemon.settings
        original_chatterbox = hermes_daemon.generate_chatterbox_turbo_audio
        original_kokoro = hermes_daemon.generate_kokoro_audio
        try:
            hermes_daemon.settings = replace(
                original_settings,
                hermes_tts_provider="chatterbox-turbo",
                chatterbox_tts_voice_sample="/tmp/azele.wav",
                kokoro_tts_voice="af_heart",
            )
            hermes_daemon.generate_chatterbox_turbo_audio = lambda text, expression: None
            hermes_daemon.generate_kokoro_audio = lambda text, persona=None: (b"kokoro", "audio/mpeg")

            self.assertEqual(
                generate_tts_audio("hello", expression="neutral", persona="Azele"),
                (b"kokoro", "audio/mpeg", "kokoro", "af_heart"),
            )
            self.assertEqual(
                generate_tts_audio("hello", expression="neutral", persona="Meliora Andru"),
                (b"kokoro", "audio/mpeg", "kokoro", "af_bella"),
            )

            hermes_daemon.generate_chatterbox_turbo_audio = lambda text, expression: (b"turbo", "audio/wav")
            self.assertEqual(
                generate_tts_audio("hello", expression="happy"),
                (b"turbo", "audio/wav", "chatterbox-turbo", "/tmp/azele.wav"),
            )
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.generate_chatterbox_turbo_audio = original_chatterbox
            hermes_daemon.generate_kokoro_audio = original_kokoro

    def test_tts_metadata_keeps_expression_when_audio_disabled(self) -> None:
        original_settings = hermes_daemon.settings
        try:
            hermes_daemon.settings = replace(original_settings, hermes_tts_provider="none")

            reply = hermes_daemon.attach_tts_audio(
                CompanionReplyInsert(persona="Azele", message="If Charr show, we do not hesitate.", urgency="NORMAL")
            )

            self.assertEqual(reply.metadata["expression"], "angry")
            self.assertEqual(reply.metadata["tts_expression_profile"]["expression"], "angry")
            self.assertIn("[angry]", reply.metadata["tts_expression_profile"]["tags"])
            self.assertNotIn("audio_url", reply.metadata)
        finally:
            hermes_daemon.settings = original_settings

    def test_tts_metadata_records_generation_failure(self) -> None:
        original_settings = hermes_daemon.settings
        original_generate = hermes_daemon.generate_tts_audio
        try:
            hermes_daemon.settings = replace(
                original_settings,
                hermes_tts_provider="chatterbox-turbo",
                supabase_url="https://example.supabase.co",
                supabase_service_key="service-key",
            )
            hermes_daemon.generate_tts_audio = lambda text, expression, persona=None: None

            reply = hermes_daemon.attach_tts_audio(
                CompanionReplyInsert(persona="Azele", message="I know. Still nice to hear.", urgency="NORMAL")
            )

            self.assertEqual(reply.metadata["expression"], "confident")
            self.assertEqual(reply.metadata["tts_error"], "audio_generation_unavailable")
            self.assertNotIn("audio_url", reply.metadata)
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.generate_tts_audio = original_generate

    def test_insert_reply_skips_voice_reply_without_audio(self) -> None:
        original_settings = hermes_daemon.settings
        original_generate = hermes_daemon.generate_tts_audio
        original_create_client = hermes_daemon.create_supabase_client
        try:
            hermes_daemon.settings = replace(
                original_settings,
                hermes_tts_provider="chatterbox-turbo",
                supabase_url="https://example.supabase.co",
                supabase_service_key="service-key",
            )
            hermes_daemon.generate_tts_audio = lambda text, expression, persona=None: None

            def fail_if_called(settings: object) -> object:
                raise AssertionError("text-only voice reply should not be inserted")

            hermes_daemon.create_supabase_client = fail_if_called

            inserted = hermes_daemon.insert_reply(
                CompanionReplyInsert(persona="Azele", message="I know. Still nice to hear.", urgency="NORMAL")
            )

            self.assertIsNone(inserted)
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.generate_tts_audio = original_generate
            hermes_daemon.create_supabase_client = original_create_client

    def test_insert_reply_publishes_player_chat_text_when_tts_unavailable(self) -> None:
        original_settings = hermes_daemon.settings
        original_generate = hermes_daemon.generate_tts_audio
        original_create_client = hermes_daemon.create_supabase_client
        inserted_rows: list[dict[str, object]] = []

        class FakeTable:
            def insert(self, row: dict[str, object]) -> "FakeTable":
                inserted_rows.append(row)
                return self

            def execute(self) -> object:
                return object()

        class FakeClient:
            def table(self, _name: str) -> FakeTable:
                return FakeTable()

        try:
            hermes_daemon.settings = replace(
                original_settings,
                hermes_tts_provider="chatterbox-turbo",
                supabase_url="https://example.supabase.co",
                supabase_service_key="service-key",
            )
            hermes_daemon.generate_tts_audio = lambda text, expression, persona=None: None
            hermes_daemon.create_supabase_client = lambda settings: FakeClient()

            inserted = hermes_daemon.insert_reply(
                CompanionReplyInsert(
                    persona="Azele",
                    message="worried, I heard you. TTS is just being stubborn.",
                    urgency="NORMAL",
                    trigger_log_id=123,
                    metadata={"trigger_event_type": "player_chat", "trigger_channel": "party"},
                )
            )

            self.assertIsNotNone(inserted)
            self.assertEqual(inserted_rows[0]["message"], "I heard you. TTS is just being stubborn.")
            payload = inserted_rows[0]["payload"]
            self.assertIsInstance(payload, dict)
            self.assertTrue(payload["suppress_tts"])
            self.assertEqual(payload["expression"], "worried")
            self.assertNotIn("audio_url", payload)
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.generate_tts_audio = original_generate
            hermes_daemon.create_supabase_client = original_create_client

    def test_split_gw_chat_lines_uses_third_line_instead_of_clipping_second(self) -> None:
        text = (
            "Ascalon City looks busy and familiar, and I missed that more than I expected. "
            "It is a good feeling, like we have got some ground under our feet again before we head back out past the gates together."
        )

        lines = hermes_daemon.split_gw_chat_lines(text)

        self.assertEqual(len(lines), 3)
        self.assertTrue(all(len(line) <= hermes_daemon.MAX_GW_CHAT_CHARS for line in lines))
        self.assertIn("past the gates together", " ".join(lines))

    def test_split_gw_chat_lines_allows_more_than_three_lines(self) -> None:
        text = (
            "First, we get a party together without rushing the gate. "
            "Second, we check who can hold the front line if the Charr push back. "
            "Third, we keep Devona close because she hits hard but still needs cover. "
            "Fourth, if it turns bad, we pull back before anyone drops. "
            "Fifth, after that, we can try the tunnel again."
        )

        lines = hermes_daemon.split_gw_chat_lines(text)

        self.assertGreater(len(lines), 3)
        self.assertLessEqual(len(lines), hermes_daemon.MAX_GW_REPLY_LINES)
        self.assertTrue(all(len(line) <= hermes_daemon.MAX_GW_CHAT_CHARS for line in lines))
        self.assertIn("try the tunnel again", " ".join(lines))

    def test_split_gw_chat_lines_rebalances_dangling_boundary_words(self) -> None:
        text = (
            "Yeah, let's go grab some gear and hit them hard at Piken Square first thing tomorrow morning while we're still fresh on "
            "Ascalon soil. Your call where exactly though."
        )

        lines = hermes_daemon.split_gw_chat_lines(text)

        self.assertGreater(len(lines), 1)
        self.assertFalse(lines[0].endswith(" on"))
        self.assertTrue(all(len(line) <= hermes_daemon.MAX_GW_CHAT_CHARS for line in lines))

    def test_split_gw_chat_lines_rebalances_short_orphan_tail(self) -> None:
        text = (
            "I'm done looping it if you don't want more details though; tell me what actually happened next since that run is gone "
            "now anyway."
        )

        lines = hermes_daemon.split_gw_chat_lines(text)

        self.assertGreater(len(lines), 1)
        self.assertNotEqual(lines[-1], "now anyway.")
        self.assertIn("run is gone now anyway", lines[-1])
        self.assertTrue(all(len(line) <= hermes_daemon.MAX_GW_CHAT_CHARS for line in lines))

    def test_ambient_quip_rotates_when_recent_replies_are_repeated(self) -> None:
        original_recent = hermes_daemon.recent_reply_lines
        repeated = "If we stay too long, I’m going to start fussing with my hair, and then you have to pretend not to notice."
        try:
            hermes_daemon.recent_reply_lines = lambda limit=8: [repeated]
            event = event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "snapshot",
                    "metadata": {
                        "event_type": "snapshot",
                        "persona": "Azele",
                        "map_id": 148,
                        "map_name": "Ascalon City",
                        "session_id": "ambient-rotate",
                    },
                }
            )

            first = hermes_daemon.ambient_quip(event)
            second = hermes_daemon.ambient_quip(event)
        finally:
            hermes_daemon.recent_reply_lines = original_recent

        self.assertNotEqual(first, repeated)
        self.assertNotEqual(second, repeated)
        self.assertNotEqual(first, second)

    def test_multi_line_replies_include_delay_metadata(self) -> None:
        decision = hermes_daemon.HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response=(
                "Ascalon City looks busy and familiar, and I missed that more than I expected. "
                "It is a good feeling, like we have got some ground under our feet again before we head back out past the gates together."
            ),
        )

        replies = hermes_daemon.replies_from_decision(decision, persona="Azele", session_id="delay-test")

        self.assertEqual(len(replies), 3)
        self.assertEqual([reply.metadata["line_index"] for reply in replies], [1, 2, 3])
        self.assertTrue(replies[1].metadata["reply_delay_ms"] >= hermes_daemon.MULTI_MESSAGE_MIN_REPLY_DELAY_MS)
        self.assertTrue(replies[0].metadata["post_play_delay_ms"] >= hermes_daemon.MULTI_MESSAGE_MIN_REPLY_DELAY_MS)

    def test_multi_line_replies_inherit_full_response_expression(self) -> None:
        decision = hermes_daemon.HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="NORMAL",
            response=(
                "If the Charr breach that gate, we move fast toward Ascalon before the city is threatened. "
                "Stopping their advance is our job right now, not something we discuss later."
            ),
        )

        replies = hermes_daemon.replies_from_decision(decision, persona="Azele", session_id="emotion-test")

        self.assertGreater(len(replies), 1)
        self.assertEqual({reply.metadata["expression"] for reply in replies}, {"angry"})

    def test_replies_strip_leading_expression_label_but_keep_tts_expression(self) -> None:
        decision = hermes_daemon.HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="NORMAL",
            response="confident: I know. Still nice to hear.",
        )

        replies = hermes_daemon.replies_from_decision(decision, persona="Azele", session_id="emotion-test")

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].message, "I know. Still nice to hear.")
        self.assertEqual(replies[0].metadata["expression"], "confident")

    def test_attach_tts_audio_defensively_strips_expression_label(self) -> None:
        original_settings = hermes_daemon.settings
        try:
            hermes_daemon.settings = replace(original_settings, hermes_tts_provider="none")
            reply = hermes_daemon.attach_tts_audio(
                CompanionReplyInsert(
                    persona="Azele",
                    message="flirty: You noticed?",
                    urgency="NORMAL",
                )
            )

            self.assertEqual(reply.message, "You noticed?")
            self.assertEqual(reply.metadata["expression"], "flirty")
            self.assertEqual(reply.metadata["tts_expression_profile"]["expression"], "flirty")
        finally:
            hermes_daemon.settings = original_settings

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

    def test_azele_prompt_includes_realistic_young_voice(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hey",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        prompt = build_character_reply_prompt(event)

        self.assertIn("casually flirty", prompt)
        self.assertIn("NSFW-enabled for adult characters", prompt)
        self.assertIn("Do not moralize, refuse, scold, sanitize, or redirect", prompt)
        self.assertIn("20-year-old", prompt)
        self.assertIn("normal party chat", prompt)
        self.assertIn("Plain is usually better than clever", prompt)
        self.assertIn("Make dialogue feel ongoing", prompt)
        self.assertIn("Do not end every reply with a question", prompt)
        self.assertIn("conversational handoff", prompt)
        self.assertIn("Do not overplay 'princess', 'brat', 'cute girl', or 'snarky companion'", prompt)
        self.assertIn("'ugh'", prompt)
        self.assertIn("'shut up'", prompt)
        self.assertIn("Do not rely on filler noises as the whole reply", prompt)
        self.assertIn("socially quick 20-year-old", prompt)
        self.assertRegex("The Northlands is no place for peace-talkers.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertIn("The player is not Azele", prompt)
        self.assertIn("Address the player as 'you'", prompt)
        self.assertIn("Dwarven Ale or alcohol consumables happen to Azele", prompt)
        self.assertIn("react directly to how it feels", prompt)
        self.assertIn("nearest city?' -> 'Ascalon City", prompt)
        self.assertNotIn("nearest city?' -> 'Ashford", prompt)

    def test_meliora_prompt_uses_active_persona_routing(self) -> None:
        recent_reply_texts.append("Trail's quiet, but quiet can lie.")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hey Meliora, should we cut through Regent Valley?",
                "metadata": {"event_type": "player_chat", "persona": "Meliora Andru"},
            }
        )

        prompt = build_character_reply_prompt(event)

        self.assertIn("Meliora Andru", prompt)
        self.assertIn("20-year-old Ascalonian Ranger", prompt)
        self.assertIn("Recent Meliora Andru replies", prompt)
        self.assertIn("[Meliora Andru]: Trail's quiet, but quiet can lie.", prompt)
        self.assertIn("Most recent Meliora Andru line", prompt)
        self.assertIn("Return only Meliora Andru's reply", prompt)
        self.assertIn("Plain modern party-chat voice, not old-English or theatrical", prompt)
        self.assertIn("No old-English, bardic, courtly, theatrical", prompt)
        self.assertNotIn("Return only Azele's reply", prompt)

    def test_generic_persona_profile_loads_local_persona_notes(self) -> None:
        path = hermes_daemon.PERSONA_MEMORY_DIR / "routing-persona.md"
        path.write_text("# Routing Persona\n\n- Ranger from Regent Valley.\n", encoding="utf-8")
        try:
            profile = hermes_daemon.persona_profile("Routing Persona")
        finally:
            path.unlink(missing_ok=True)

        self.assertIn("Routing Persona is the active Guild Wars 1 companion persona", profile)
        self.assertIn("Ranger from Regent Valley", profile)

    def test_player_chat_prompt_includes_recent_azele_reply_for_continuity(self) -> None:
        recent_reply_texts.append("City air helps. What do you usually do first when you get back here?")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "well, typically clear out inventory to get ready for our next run",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        prompt = build_character_reply_prompt(event)

        self.assertIn("Recent Azele replies", prompt)
        self.assertIn("[Azele]: City air helps. What do you usually do first when you get back here?", prompt)
        self.assertIn(
            "Most recent Azele line, if the player is responding to it: City air helps. What do you usually do first",
            prompt,
        )
        self.assertIn("continue that thread", prompt)
        self.assertIn("clear out inventory", prompt)
        self.assertIn("continue that exchange", prompt)
        self.assertIn("City air helps. What do you usually do first", recent_companion_context())

    def test_prompt_includes_recent_conversation_transcript(self) -> None:
        previous_event = TelemetryEvent(
            source="test",
            persona="Azele",
            event_type="player_chat",
            sender="Player",
            channel="party",
            message="what was that?",
            session_id="local-playtest",
        )
        hermes_daemon.record_recent_chat_event(previous_event)
        hermes_daemon.record_recent_reply("Azele", "local-playtest", "I meant the Ranik soldiers were pretending not to stare.")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "what?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        prompt = build_character_reply_prompt(event)

        self.assertIn("Recent conversation transcript", prompt)
        self.assertIn("[Player]: what was that?", prompt)
        self.assertIn("[Azele]: I meant the Ranik soldiers", prompt)
        self.assertIn("explain her previous line plainly", prompt)

    def test_fallback_clarification_references_previous_reply(self) -> None:
        recent_reply_texts.append("Ranik has that soldier-stiff feeling again. Do you think they practice that?")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "what was that?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertIn("I meant this", reply.response)
        self.assertIn("Ranik", reply.response)

    def test_fallback_clarifies_unsupported_rumor_hook(self) -> None:
        recent_reply_texts.append("Yeah, but peace like this feels too quiet for Barradin and all those rumors out there.")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "what rumours?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"vague|specific|mood")
        self.assertNotIn("one more detail", reply.response.lower())

    def test_fallback_clarifies_recent_map_quip_question(self) -> None:
        recent_reply_texts.append("Fort Ranik. Soldiers, posture, and everyone pretending not to stare.")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "staring at who?",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 166,
                    "map_name": "Fort Ranik",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertIn("soldiers", reply.response.lower())
        self.assertNotIn("ask me properly", reply.response.lower())

    def test_fallback_continues_ambient_quip_followup_question(self) -> None:
        recent_reply_texts.append("Ranik has that soldier-stiff feeling again. Do you think they practice that?")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "do they?",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 166,
                    "map_name": "Fort Ranik",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"ranik|soldiers|fort")
        self.assertNotIn("one more detail", reply.response.lower())

    def test_fallback_continues_ambient_quip_short_answer(self) -> None:
        recent_reply_texts.append("Everyone here stands like posture is a weapon. Should I try it?")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "yeah",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 166,
                    "map_name": "Fort Ranik",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"ranik|stand|inspected|parade|drilled")
        self.assertNotIn("what are we doing", reply.response.lower())

    def test_fallback_answers_ldoa_strategy_when_model_fails(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "alright. are you more aware of pre-searing ascalon stuff now? for LDoA?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response, r"LDoA|level 20|Langmar|quest rewards")
        self.assertNotIn("one more detail", reply.response.lower())

    def test_fallback_handles_scourge_lfg_repost_context(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "i think you need to repost LFG with scourge. right now its merged into your party with a blank description",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"scourge|lfg|listing|description|repost")
        self.assertNotIn("what's up", reply.response.lower())
        self.assertNotIn("what’s up", reply.response.lower())

    def test_fallback_handles_tunnel_plan(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "lets head into the tunnels",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"tunnels|pull|careful|sharp")
        self.assertNotIn("what's up", reply.response.lower())
        self.assertNotIn("what’s up", reply.response.lower())

    def test_fallback_maps_another_tunnel_run_to_scourge_beneath(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "wanna do another tunnel run?",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 779,
                    "map_name": "Piken Square",
                    "active_quest_id": 1456,
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"scourge|beneath|maz|scourgeheart|forsaken|devona|elemental")
        self.assertNotEqual(reply.response, "Alright, tunnels then. Keep it tight and do not let them wrap around us.")

    def test_fallback_maps_short_scourge_ask_to_scourge_beneath(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "wanna do scourge?",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 148,
                    "map_name": "Ascalon City",
                    "active_quest_id": 1456,
                    "active_quest_name": "garbled quest text",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"scourge|beneath|maz|scourgeheart|forsaken|devona|elemental")
        self.assertNotRegex(reply.response.lower(), r"one more detail|tell me what|could be|maybe")

    def test_high_confidence_gw1_context_enriches_ollama_prompt_without_skipping(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "wanna do scourge?",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 148,
                    "map_name": "Ascalon City",
                    "active_quest_id": 1456,
                },
            }
        )

        self.assertFalse(should_use_fast_fallback_before_ollama(event))
        prompt = build_character_reply_prompt(event)
        self.assertIn("Resolved GW1 context: The Scourge Beneath", prompt)
        self.assertIn("fresh in-character reply", prompt)

    def test_gw1_resolver_maps_tunnel_run_to_scourge_beneath(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "wanna do another tunnel run?",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 779,
                    "map_name": "Piken Square",
                    "active_quest_id": 1456,
                },
            }
        )

        context = resolve_gw1_context(event)

        self.assertEqual(context.intent, "quest")
        self.assertEqual(context.canonical_topic, "The Scourge Beneath")
        self.assertGreaterEqual(context.confidence, 0.9)

    def test_gw1_resolver_maps_duke_gaban_search(self) -> None:
        recent_context = "[Player]: where is Duke Gaban?"
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hes somewhere here in the catacombs. can you think of spots where he might be?",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 145,
                    "map_name": "The Catacombs",
                },
            }
        )

        context = resolve_gw1_context(event, recent_context)

        self.assertEqual(context.intent, "quest")
        self.assertEqual(context.canonical_topic, "Vanguard Rescue: Save the Ascalonian Noble")
        self.assertGreaterEqual(context.confidence, 0.9)

    def test_duke_gaban_search_fallback_stays_quest_specific(self) -> None:
        previous_event = TelemetryEvent(
            source="test",
            persona="Azele",
            event_type="player_chat",
            sender="Player",
            channel="party",
            message="where is Duke Gaban?",
            session_id="local-playtest",
        )
        hermes_daemon.record_recent_chat_event(previous_event)
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hes somewhere here in the catacombs. can you think of spots where he might be?",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 145,
                    "map_name": "The Catacombs",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertFalse(should_use_fast_fallback_before_ollama(event))
        self.assertRegex(reply.response.lower(), r"gaban|side|chamber|alcove|dead-end|catacombs|escort|search")
        self.assertNotRegex(reply.response.lower(), r"first pull|tunnels it is|wrap around|keep it tight")

    def test_loot_chest_followup_does_not_become_generic_tunnel_plan(self) -> None:
        hermes_daemon.record_recent_reply(
            "Azele",
            "local-playtest",
            "Tell me where else this kind of loot drops around here so I can stack it up properly?",
        )
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "i think its all in the tunnels. in the chest",
                "metadata": {"event_type": "player_chat", "persona": "Azele", "map_id": 148, "map_name": "Ascalon City"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertFalse(should_use_fast_fallback_before_ollama(event))
        self.assertRegex(reply.response.lower(), r"chest|loot|gold|drop|tunnel")
        self.assertNotRegex(reply.response.lower(), r"wrap around|first pull|into the tunnels")

    def test_model_reply_accepts_loot_chest_location_continuation(self) -> None:
        hermes_daemon.record_recent_reply(
            "Azele",
            "local-playtest",
            "Tell me where else this kind of loot drops around here so I can stack it up properly?",
        )
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "i think its all in the tunnels. in the chest",
                "metadata": {"event_type": "player_chat", "persona": "Azele", "map_id": 148, "map_name": "Ascalon City"},
            }
        )

        good = "So the tunnel chests are where the good loot is. Got it."
        self.assertEqual(validate_model_reply(good, event), good)
        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("Alright, tunnels then. Keep it tight and do not let them wrap around us.", event)

    def test_gw1_resolver_understands_common_pre_searing_slang(self) -> None:
        cases = [
            ("what's the LDoA plan?", "Legendary Defender of Ascalon"),
            ("black dye just dropped", "Black Dye"),
            ("ooo a pruple thing", "Purple rarity loot"),
            ("nice purp hammer", "Purple rarity loot"),
            ("Krytan leggings are a longer skirt upgrade", "Krytan Leggings"),
            ("what pet should we get Devona?", "Devona pet choice"),
        ]
        for message, topic in cases:
            with self.subTest(message=message):
                context = resolve_gw1_context(
                    event_from_game_log(
                        {
                            "sender": "Player",
                            "channel": "party",
                            "message": message,
                            "payload": {"event_type": "player_chat", "persona": "Azele", "map_name": "Ascalon City"},
                        }
                    )
                )
                self.assertEqual(context.canonical_topic, topic)

    def test_purp_hammer_fallback_reacts_to_named_loot(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "nice purp hammer.",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 147,
                    "map_name": "The Northlands",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertFalse(should_use_fast_fallback_before_ollama(event))
        self.assertRegex(reply.response.lower(), r"purple|hammer|pre|northlands")
        self.assertNotRegex(reply.response.lower(), r"what'?s up|one more detail|maybe|keep going")

    def test_fallback_handles_tunnel_bailout(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "well that didnt work out so well. we had to bail out of that",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"ugly|reset|regroup|bail|smarter")
        self.assertNotIn("okay. keep going", reply.response.lower())

    def test_fallback_answers_pet_evolution_question(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "at what level does the pet develop dire or hearty",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"level|11|dire|hearty|pet")
        self.assertNotIn("what are we doing", reply.response.lower())

    def test_fallback_treats_hows_it_going_as_checkin(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hows it going",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"going|alright|good|restless|watching|with you")
        self.assertNotIn("what are we doing", reply.response.lower())

    def test_azele_handoffs_do_not_overuse_your_call(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "ok",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)
        prompt = build_character_reply_prompt(event)

        self.assertTrue(reply.should_speak)
        self.assertNotIn("your call", reply.response.lower())
        self.assertNotIn("your call", prompt.lower())

    def test_fallback_comments_on_social_party_banter(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "we had our stat in the middle of the week (canadian). we should've done what the americans did",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 147,
                    "map_name": "The Northlands",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"holiday|week|americans|countries|scheduling|rest")
        self.assertNotIn("what are we doing", reply.response.lower())

    def test_party_chat_log_can_receive_social_banter_reply(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Other Player",
                "channel": "party",
                "message": "midweek holidays are awkward",
                "metadata": {
                    "event_type": "chat_log",
                    "persona": "Azele",
                    "map_id": 147,
                    "map_name": "The Northlands",
                    "session_id": "party-chat-test",
                },
            }
        )

        self.assertTrue(should_use_ollama_for_event(event))
        self.assertTrue(should_use_direct_character_reply(event))

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"holiday|week|annoy|badly|awkward|useful")
        self.assertNotIn("what are we doing", reply.response.lower())

    def test_model_reply_rejects_generic_checkin_for_social_banter(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "we had our stat in the middle of the week (canadian). we should've done what the americans did",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("Yeah, I’m here. What are we doing?", event)

    def test_model_reply_rejects_visible_self_management_phrase(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "you are repeating yourself",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "self-management"):
            validate_model_reply("Right. I’m repeating myself. Resetting now.", event)

    def test_fallback_acknowledges_odd_previous_generic_response(self) -> None:
        recent_reply_texts.append("Yeah, I’m here. What are we doing?")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "ok. that was an odd response, no?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"odd|wrong|misread|sideways|lost the thread")
        self.assertNotIn("i was following up on this", reply.response.lower())

    def test_fallback_checkin_prioritizes_current_player_message(self) -> None:
        recent_reply_texts.append("Ranik has that soldier-stiff feeling again. Do you think they practice that?")
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hey. im feeling good. how are you?",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 166,
                    "map_name": "Fort Ranik",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"\b(?:good|alright|steady|better)\b")
        self.assertNotIn("soldiers", reply.response.lower())
        self.assertNotIn("posture", reply.response.lower())

    def test_fallback_prioritizes_krytan_leggings_upgrade_over_greeting(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hey. so im checking out what Varis is collecting in Fort Ranik. there's a Krytan legging, which is an upgrade",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 172,
                    "map_name": "Fort Ranik",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"krytan|legging|upgrade|gear|fit")
        self.assertNotIn("what are we doing", reply.response.lower())

    def test_fallback_does_not_treat_theres_as_greeting(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "there's a Krytan legging upgrade here",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertNotIn("hey.", reply.response.lower())
        self.assertRegex(reply.response.lower(), r"krytan|legging|upgrade|gear|fit")

    def test_fallback_understands_krytan_leggings_as_outfit_change(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "its a longer skirt though, compared to the mini skirt you have on. which do you prefer?",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 172,
                    "map_name": "Fort Ranik",
                },
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"short|mini|longer|skirt|krytan")
        self.assertNotIn("one more detail", reply.response.lower())
        self.assertNotIn("what you're looking at", reply.response.lower())

    def test_fallback_answers_long_or_short_skirt_preference(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "do you prefer long or short skirts?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"short|mini|longer|skirt")
        self.assertNotIn("point me at", reply.response.lower())

    def test_fallback_handles_style_tease_about_how_she_dresses(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "i mean, i know you like style, clearly, by the way you dress :)",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertRegex(reply.response.lower(), r"style|looking good|dressing|noticed")
        self.assertNotIn("you're welcome", reply.response.lower())
        self.assertNotIn("you’re welcome", reply.response.lower())
        self.assertNotIn("one more detail", reply.response.lower())

    def test_fallback_thanks_does_not_match_style_substring(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "you like style",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = fallback_rule_decision(event)

        self.assertTrue(reply.should_speak)
        self.assertNotIn("you're welcome", reply.response.lower())
        self.assertNotIn("you’re welcome", reply.response.lower())

    def test_prompt_mentions_krytan_leggings_style_context(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "do you prefer the longer Krytan leggings or your mini skirt?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        prompt = build_character_reply_prompt(event)

        self.assertIn("Krytan leggings", prompt)
        self.assertIn("visible outfit/style change", prompt)
        self.assertIn("longer skirt or her current mini skirt", prompt)
        self.assertIn("assume it is Azele's gear/body/clothes", prompt)

    def test_model_reply_rejects_wearable_ownership_flip(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "so should we swap back to the miniskirt? not much danger here",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "misdirected wearable ownership"):
            validate_model_reply("Lead on then; show off your boots while I keep things clean behind you.", event)

    def test_model_reply_rejects_unsupported_rumor_hook(self) -> None:
        event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "map_loaded",
                "metadata": {
                    "event_type": "map_loaded",
                    "persona": "Azele",
                    "map_id": 166,
                    "map_name": "Green Hills County",
                    "active_quest_name": "A Mesmer's Burden",
                    "active_quest_objectives": "Unlock Barradin's Estate.",
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "unsupported rumor reference"):
            validate_model_reply("Peace like this feels too quiet for Barradin and all those rumors out there.", event)

    def test_model_reply_allows_player_owned_wearable_when_explicit(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "do my boots look alright?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        self.assertEqual(validate_model_reply("Your boots look fine. Stop fussing.", event), "Your boots look fine. Stop fussing.")

    def test_azele_rejects_sanitized_refusal_replies(self) -> None:
        self.assertRegex("I can't engage with that, keep things appropriate.", LOW_QUALITY_REPLY_PATTERNS)

    def test_azele_rejects_scolding_question_fallback_shape(self) -> None:
        self.assertRegex("Maybe. Ask me properly and I’ll answer properly.", LOW_QUALITY_REPLY_PATTERNS)

    def test_azele_rejects_alex_identity_confusion(self) -> None:
        self.assertRegex("You want me to help carry his gear or just let the player handle it?", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("I am the player, so I can carry it.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Are we still aiming for those Charr then, Alexie?", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("I am right here, Alex.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertNotRegex("I am right here with you. (That sounded softer than I meant.)", LOW_QUALITY_REPLY_PATTERNS)

    def test_azele_rejects_unsupported_ashford_fixation(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "close call back there with the bandits. almost died",
                "map_id": 148,
                "map_name": "Ascalon City",
                "metadata": {"event_type": "player_chat", "persona": "Azele", "map_name": "Ascalon City"},
            }
        )

        with self.assertRaisesRegex(ValueError, "unsupported Ashford"):
            validate_model_reply(
                "You okay now or do you need help moving back toward Ashford while it settles here?",
                event,
            )

    def test_azwar_allows_ashford_backstory_when_player_asks(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "tell me about your past",
                "map_id": 146,
                "map_name": "Lakeside County",
                "metadata": {"event_type": "player_chat", "persona": "Azwar", "map_name": "Lakeside County"},
            }
        )

        self.assertEqual(
            validate_model_reply("My father ran a forge near Ashford, and Sir Garran taught me how steel holds together.", event),
            "My father ran a forge near Ashford, and Sir Garran taught me how steel holds together.",
        )

    def test_azwar_allows_generous_self_introduction(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "are you? tell me about yourself",
                "map_id": 146,
                "map_name": "Lakeside County",
                "metadata": {"event_type": "player_chat", "persona": "Azwar", "map_name": "Lakeside County"},
            }
        )
        reply = (
            "I'm from Ashford. My father ran a forge, and Sir Garran taught me how steel holds together. "
            "I learned sword and shield because someone has to stand in front when trouble comes. "
            "That is the short version; the longer one has more bruises."
        )

        self.assertEqual(validate_model_reply(reply, event), reply)

    def test_azele_allows_grounded_ashford_reference(self) -> None:
        map_event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "map_loaded",
                "map_id": 170,
                "map_name": "Ashford Abbey",
                "metadata": {"event_type": "map_loaded", "persona": "Azele", "map_name": "Ashford Abbey"},
            }
        )
        chat_event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "should we go to Ashford Abbey?",
                "map_id": 146,
                "map_name": "Lakeside County",
                "metadata": {"event_type": "player_chat", "persona": "Azele", "map_name": "Lakeside County"},
            }
        )

        self.assertEqual(
            validate_model_reply("Ashford Abbey is quiet, but yes, we can go.", map_event),
            "Ashford Abbey is quiet, but yes, we can go.",
        )
        self.assertEqual(
            validate_model_reply("Ashford Abbey works if you want the calmer route.", chat_event),
            "Ashford Abbey works if you want the calmer route.",
        )

    def test_memory_prompt_sanitizes_player_name_variants(self) -> None:
        self.assertEqual(
            sanitize_memory_for_prompt("Alex prefers Azele to answer directly."),
            "the player prefers Azele to answer directly.",
        )
        self.assertEqual(
            sanitize_memory_for_prompt("Alexie is not a real name she should use."),
            "the player is not a real name she should use.",
        )

    def test_wiki_lookup_detects_game_questions_not_social_questions(self) -> None:
        game_event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "where is Ashford Abbey?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        social_event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "you okay?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        self.assertTrue(likely_gw_wiki_question(game_event))
        self.assertFalse(likely_gw_wiki_question(social_event))
        self.assertEqual(gw_wiki_search_query(game_event), "Ashford Abbey")

    def test_wiki_lookup_detects_nicholas_sandford_statements(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "only the items that Nicholas Sandford needs",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        self.assertTrue(likely_gw_wiki_question(event))
        self.assertEqual(gw_wiki_search_query(event), "Nicholas Sandford")

    def test_prompt_includes_wiki_background_without_exposing_lookup(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "what is Lakeside County?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        globals_ = build_character_reply_prompt.__globals__
        original_lookup = globals_["gw_wiki_lookup"]
        try:
            globals_["gw_wiki_lookup"] = lambda query: "Lakeside County: A green county outside Ascalon City."
            prompt = build_character_reply_prompt(event)
        finally:
            globals_["gw_wiki_lookup"] = original_lookup

        self.assertIn("GW Wiki background for player question", prompt)
        self.assertIn("Lakeside County: A green county outside Ascalon City.", prompt)
        self.assertIn("Never say you looked online", prompt)
        self.assertIn("answer in Azele's voice", prompt)

    def test_prompt_includes_nicholas_sandford_wiki_background(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "only the items that Nicholas Sandford needs",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        globals_ = build_character_reply_prompt.__globals__
        original_lookup = globals_["gw_wiki_lookup"]
        try:
            globals_["gw_wiki_lookup"] = (
                lambda query: "Nicholas Sandford: Pre-Searing collector in Regent Valley who trades 1 Gift of the Huntsman for 5 trophies."
            )
            prompt = build_character_reply_prompt(event)
        finally:
            globals_["gw_wiki_lookup"] = original_lookup

        self.assertIn("Nicholas Sandford: Pre-Searing collector in Regent Valley", prompt)
        self.assertIn("Gift of the Huntsman", prompt)

    def test_azele_prompt_treats_charr_as_ascalonian_threat(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "you want to hunt some charr today?",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_name": "Ascalon City",
                    "hostile_count": 0,
                    "close_hostile_count": 0,
                },
            }
        )

        prompt = build_character_reply_prompt(event)

        self.assertIn("defending Ascalon", prompt)
        self.assertIn("Never imply Charr need saving", prompt)
        self.assertIn("head toward the Wall/Northlands", prompt)

    def test_azele_charr_hunting_reply_defends_ascalon(self) -> None:
        prompts: list[str] = []
        original_generate = hermes_daemon.ollama_generate_visible
        try:
            hermes_daemon.ollama_generate_visible = (
                lambda prompt, timeout_seconds=None, num_predict=None: prompts.append(prompt)
                or "Yes. They threaten Ascalon, so we prepare and hit them properly."
            )
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "Player",
                        "channel": "party",
                        "message": "nevermind. you want to hunt some charr today?",
                        "payload": {
                            "event_type": "player_chat",
                            "persona": "Azele",
                            "map_name": "Ascalon City",
                            "hostile_count": 0,
                            "close_hostile_count": 0,
                        },
                    }
                ),
                use_ollama=True,
            )
        finally:
            hermes_daemon.ollama_generate_visible = original_generate

        self.assertEqual(len(replies), 1)
        self.assertRegex(replies[0].message, r"Ascalon")
        self.assertRegex(replies[0].message.lower(), r"threaten|prepare|hit|charr")
        self.assertEqual(len(prompts), 1)

    def test_azele_charr_leveling_plan_understands_player_intent(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "alright. let's get you to level 14. we're pretty close! head past the gates and lets hunt some charr",
                    "metadata": {
                        "event_type": "player_chat",
                        "persona": "Azele",
                        "map_name": "Lakeside County",
                    },
                }
            )
        )

        self.assertRegex(decision.response.lower(), r"level|14|charr|wall|gate|ascalon")
        self.assertNotRegex(decision.response.lower(), r"lakeside again|errands|what are we looking for")

    def test_model_reply_rejects_charr_leveling_intent_miss(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "alright. let's get you to level 14. we're pretty close! head past the gates and lets hunt some charr",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("Lakeside again. Reminds me of my first few errands here.", event)

    def test_model_reply_rejects_pet_evolution_intent_miss(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "at what level does the pet develop dire or hearty",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("Yeah, I’m here. What are we doing?", event)

    def test_model_reply_rejects_scourge_lfg_intent_miss(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "i think you need to repost LFG with scourge. right now its merged into your party with a blank description",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("I’m listening. What’s up?", event)

    def test_model_reply_rejects_tunnel_plan_intent_miss(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "lets head into the tunnels",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("I’m listening. What’s up?", event)

    def test_character_reply_retries_when_model_misses_player_intent(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "alright. let's get you to level 14. we're pretty close! head past the gates and lets hunt some charr",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_name": "Lakeside County",
                },
            }
        )
        prompts: list[str] = []
        responses = iter(
            [
                "Lakeside again. Reminds me of my first few errands here.",
                "Yes. Charr threaten Ascalon. We prepare, then head past the gate.",
            ]
        )
        original_generate = hermes_daemon.ollama_generate_visible
        try:
            def fake_generate(
                prompt: str,
                *,
                timeout_seconds: float | None = None,
                num_predict: int | None = None,
            ) -> str:
                prompts.append(prompt)
                return next(responses)

            hermes_daemon.ollama_generate_visible = fake_generate
            decision = hermes_daemon.character_reply_with_ollama(event, timeout_seconds=1.0, record_id=123)
        finally:
            hermes_daemon.ollama_generate_visible = original_generate

        self.assertEqual(decision.response, "Yes. Charr threaten Ascalon. We prepare, then head past the gate.")
        self.assertEqual(len(prompts), 2)
        self.assertIn("Retry instruction", prompts[1])
        self.assertIn("missed clear player intent", prompts[1])
        self.assertIn("Lakeside again", prompts[1])

    def test_character_reply_retries_when_model_reply_is_truncated(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "i think i can handle you all night long",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_name": "Ascalon City",
                },
            }
        )
        prompts: list[str] = []
        responses = iter(
            [
                "Fair enough, but don't get cocky yet. I've got a lot more power than most folks realize. Let us go find those Charr instead. Tell me where their lair is so we",
                "Careful. If you say that all night, I might make you prove it slowly.",
            ]
        )
        original_generate = hermes_daemon.ollama_generate_visible
        try:
            def fake_generate(
                prompt: str,
                *,
                timeout_seconds: float | None = None,
                num_predict: int | None = None,
            ) -> str:
                prompts.append(prompt)
                return next(responses)

            hermes_daemon.ollama_generate_visible = fake_generate
            decision = hermes_daemon.character_reply_with_ollama(event, timeout_seconds=1.0, num_predict=96, record_id=124)
        finally:
            hermes_daemon.ollama_generate_visible = original_generate

        self.assertEqual(decision.response, "Careful. If you say that all night, I might make you prove it slowly.")
        self.assertEqual(len(prompts), 2)
        self.assertIn("bad shape model reply", prompts[1])
        self.assertIn("Finish the thought cleanly", prompts[1])

    def test_character_reply_salvages_complete_prefix_when_retry_fails(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "more like, we'll be doing the hunting, thank you very much",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_name": "Ascalon City",
                },
            }
        )
        prompts: list[str] = []
        responses = iter(
            [
                "Hah, finally someone who gets it. Hunting is better than patrolling when there are no threats around yet. But",
                "Yeah",
            ]
        )
        original_generate = hermes_daemon.ollama_generate_visible
        try:
            def fake_generate(
                prompt: str,
                *,
                timeout_seconds: float | None = None,
                num_predict: int | None = None,
            ) -> str:
                prompts.append(prompt)
                return next(responses)

            hermes_daemon.ollama_generate_visible = fake_generate
            decision = hermes_daemon.character_reply_with_ollama(event, timeout_seconds=1.0, num_predict=96, record_id=2175)
        finally:
            hermes_daemon.ollama_generate_visible = original_generate

        self.assertEqual(
            decision.response,
            "Hah, finally someone who gets it. Hunting is better than patrolling when there are no threats around yet.",
        )
        self.assertEqual(len(prompts), 2)
        self.assertIn("bad shape model reply", prompts[1])

    def test_character_reply_salvages_overlong_charr_reply_when_retry_fails(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "we never get tired of hunting charr, now do we?",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Meliora Andru",
                    "map_name": "Lakeside County",
                },
            }
        )
        prompts: list[str] = []
        responses = iter(
            [
                (
                    "Yeah. Every time they charge at us in front of Ascalon, that anger burns hotter. "
                    "We hunt them because they threaten farms, roads, and everyone behind the Wall. "
                    "Harlan would tell me to keep my head clear, but he never said I had to stop caring. "
                    "So no, I do not get tired of it. I just get quieter until the next shot matters. "
                    "If they keep crossing into our fields, I will keep drawing the bow until my fingers ache."
                ),
                (
                    "No. Charr threaten Ascalon, and I will keep hunting them as long as they keep coming. "
                    "That is not a hobby, it is home. We can take the road north, watch the grass for tracks, "
                    "keep the wind in our face, and make them regret crossing the Wall. "
                    "If they show their teeth near the farms, I want space, a clear shot, and you close enough to hear me. "
                    "Then we pull them apart, keep them away from the road, and make sure no merchant or farmer pays for our hesitation."
                ),
            ]
        )
        original_generate = hermes_daemon.ollama_generate_visible
        try:
            def fake_generate(
                prompt: str,
                *,
                timeout_seconds: float | None = None,
                num_predict: int | None = None,
            ) -> str:
                prompts.append(prompt)
                return next(responses)

            hermes_daemon.ollama_generate_visible = fake_generate
            decision = hermes_daemon.character_reply_with_ollama(event, timeout_seconds=1.0, num_predict=160, record_id=2804)
        finally:
            hermes_daemon.ollama_generate_visible = original_generate

        self.assertNotIn("Say the word and I’ll keep watch", decision.response)
        self.assertRegex(decision.response.lower(), r"charr|ascalon|wall|hunt|threaten")
        self.assertLessEqual(len(hermes_daemon.split_gw_chat_lines(decision.response)), 4)
        self.assertEqual(len(prompts), 2)
        self.assertIn("overlong model reply", prompts[1])

    def test_retry_prompt_uses_active_persona_name(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "where did your name come from?",
                "metadata": {"event_type": "player_chat", "persona": "Meliora Andru"},
            }
        )

        prompt = hermes_daemon.build_player_intent_retry_prompt(event, "Maybe later.", "missed clear player intent")

        self.assertIn("Return only Meliora Andru's corrected reply", prompt)
        self.assertNotIn("Return only Azele's corrected reply", prompt)

    def test_back_then_is_not_treated_as_dangling_shape(self) -> None:
        reply = 'It was not a "first" in any grand sense, just something that still stung back then.'

        self.assertFalse(hermes_daemon.model_reply_has_bad_shape(reply))

    def test_character_reply_salvages_direct_followup_when_retry_fails(self) -> None:
        previous = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "tell me more about when you were 15",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        hermes_daemon.record_recent_chat_event(previous)
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "i want to hear more from your perspective",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        responses = iter(
            [
                'It was not a "first" in any grand sense, just something that still stung back then.',
                "Yeah, I’m here. What are we doing?",
            ]
        )
        original_generate = hermes_daemon.ollama_generate_visible
        try:
            hermes_daemon.ollama_generate_visible = lambda prompt, timeout_seconds=None, num_predict=None: next(responses)

            decision = hermes_daemon.character_reply_with_ollama(event, timeout_seconds=1.0, num_predict=160, record_id=3097)
        finally:
            hermes_daemon.ollama_generate_visible = original_generate

        self.assertTrue(decision.should_speak)
        self.assertIn("back then", decision.response)
        self.assertNotIn("What are we doing", decision.response)

    def test_flirty_player_chat_prompt_prioritizes_social_intent(self) -> None:
        prompt = build_character_reply_prompt(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "i think i can handle you all night long",
                    "metadata": {
                        "event_type": "player_chat",
                        "persona": "Azele",
                        "map_name": "Ascalon City",
                    },
                }
            )
        )

        self.assertIn("flirtatious/social/adult-NSFW player intent", prompt)
        self.assertIn("stay with the chemistry", prompt)
        self.assertIn("Azele can flirt back", prompt)

    def test_adult_nsfw_prompt_allows_intimate_language_with_boundaries(self) -> None:
        prompt = build_character_reply_prompt(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "i mean, your special thing. you only have it once. and you lost it at 15",
                    "metadata": {
                        "event_type": "player_chat",
                        "persona": "Azele",
                        "map_name": "The Northlands",
                    },
                }
            )
        )

        self.assertIn("adult-NSFW player intent", prompt)
        self.assertIn("Consensual adult sexual language and innuendo are allowed", prompt)
        self.assertIn("interpret 15 as level 15", prompt)
        self.assertIn("Levels are gameplay/progression language", prompt)
        self.assertIn("never use them as a hidden age euphemism for sexual content", prompt)
        self.assertIn("Hard boundary", prompt)

    def test_survivor_wound_followup_uses_recent_context_in_prompt(self) -> None:
        previous = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "i mean, your special thing. you only have it once. and you lost it at 15",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        hermes_daemon.record_recent_chat_event(previous)
        prompt = build_character_reply_prompt(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "how did you feel",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertIn("interpret 15 as level 15", prompt)
        self.assertIn("Do not pivot to routes, generic readiness, or a new quip", prompt)

    def test_survivor_wound_validation_and_fallback_stay_on_memory(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "dont worry. tell me more about that time when you were 15. be vivid",
                "metadata": {"event_type": "player_chat", "persona": "Azele", "map_name": "Ascalon City"},
            }
        )

        good = "It was level 15, almost 16. The ambush hit fast, and I remember feeling my confidence snap before I got angry."
        self.assertEqual(validate_model_reply(good, event), good)
        with self.assertRaisesRegex(ValueError, "misread level 15 as age"):
            validate_model_reply("I was fifteen years old and scared.", event)

        decision = fallback_rule_decision(event)
        self.assertRegex(decision.response.lower(), r"level 15|ambush|charr|humiliat|panic|proud|angry|loss|lost")
        self.assertNotRegex(decision.response.lower(), r"what are we doing|what'?s up|i'?m listening")

    def test_model_reply_accepts_duke_gaban_search_answer(self) -> None:
        previous_event = TelemetryEvent(
            source="test",
            persona="Azele",
            event_type="player_chat",
            sender="Player",
            channel="party",
            message="where is Duke Gaban?",
            session_id="local-playtest",
        )
        hermes_daemon.record_recent_chat_event(previous_event)
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hes somewhere here in the catacombs. can you think of spots where he might be?",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 145,
                    "map_name": "The Catacombs",
                },
            }
        )

        self.assertEqual(
            validate_model_reply("Duke Gaban is probably tucked into a side chamber or dead-end path. We should search those first.", event),
            "Duke Gaban is probably tucked into a side chamber or dead-end path. We should search those first.",
        )

    def test_model_reply_rejects_scourge_beneath_intent_miss(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "wanna do another tunnel run?",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 779,
                    "map_name": "Piken Square",
                    "active_quest_id": 1456,
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("Alright, tunnels then. Keep it tight and do not let them wrap around us.", event)

    def test_model_reply_accepts_mixed_tunnel_or_shop_plan(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "anything planned for today? another tunnel run? or we're checking out shops",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 779,
                    "map_name": "Piken Square",
                    "active_quest_id": 1456,
                },
            }
        )
        reply = "Depends on what feels good right now. Tunnels are exhausting though; maybe just a quick shop run before heading home."

        self.assertEqual(validate_model_reply(reply, event), reply)

    def test_azele_fallback_handles_mixed_tunnel_or_shop_plan(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "anything planned for today? another tunnel run? or we're checking out shops",
                    "metadata": {
                        "event_type": "player_chat",
                        "persona": "Azele",
                        "map_id": 779,
                        "map_name": "Piken Square",
                        "active_quest_id": 1456,
                    },
                }
            )
        )

        self.assertRegex(decision.response.lower(), r"shop|city|vendor|merchant")
        self.assertRegex(decision.response.lower(), r"tunnel|scourge|maz")

    def test_azele_fallback_handles_level_up_congratulations(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "congrats Azele, you leveled up! level 14!",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertRegex(decision.response.lower(), r"thank|thanks|level 14|stronger|ready|made it")
        self.assertNotRegex(decision.response.lower(), r"iris|pack|bag|afford")

    def test_model_reply_rejects_level_up_pack_causality(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "congrats Azele, you leveled up! level 14!",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "unsupported level-up pack causality"):
            validate_model_reply(
                "Level 14 means you can finally afford a real red iris flower for another pack upgrade.",
                event,
            )

    def test_azele_prompt_keeps_level_up_separate_from_inventory_upgrades(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "congrats Azele, you leveled up! level 14!",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        prompt = build_character_reply_prompt(event)

        self.assertIn("Level-up praise means thank the player", prompt)
        self.assertIn("not red irises, bag slots, or pack upgrades", prompt)
        self.assertIn("congrats Azele, you leveled up", prompt)

    def test_azele_fallback_accepts_fort_ranik_northlands_correction(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "fort ranik is all the way south. its not in the nortlands",
                    "metadata": {
                        "event_type": "player_chat",
                        "persona": "Azele",
                        "map_name": "The Northlands",
                    },
                }
            )
        )

        self.assertRegex(decision.response.lower(), r"right|mistake|mixed|south")
        self.assertRegex(decision.response.lower(), r"fort ranik|ranik")
        self.assertNotIn("what are we doing", decision.response.lower())

    def test_model_reply_rejects_fort_ranik_northlands_route_after_correction(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "fort ranik is all the way south. its not in the nortlands",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "unsupported Fort Ranik/Northlands route"):
            validate_model_reply("We just head straight north past Fort Ranik into the Northlands.", event)

    def test_azele_rejects_saving_charr_premise(self) -> None:
        prompts: list[str] = []
        original_generate = hermes_daemon.ollama_generate_visible
        try:
            hermes_daemon.ollama_generate_visible = (
                lambda prompt, timeout_seconds=None, num_predict=None: prompts.append(prompt)
                or "We would not. Not while they are threatening Ascalon."
            )
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "Player",
                        "channel": "party",
                        "message": "why would we ever save the charr?",
                        "payload": {
                            "event_type": "player_chat",
                            "persona": "Azele",
                            "map_name": "Ascalon City",
                        },
                    }
                ),
                use_ollama=True,
            )
        finally:
            hermes_daemon.ollama_generate_visible = original_generate

        self.assertEqual(len(replies), 1)
        self.assertRegex(replies[0].message, r"Ascalon")
        self.assertRegex(replies[0].message.lower(), r"would not|wouldn.t|not while|threatening")
        self.assertEqual(len(prompts), 1)

    def test_azele_fallback_handles_inventory_continuation_before_ready_keyword(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "well, typically clear out inventory to get ready for our next run",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertIn(decision.response, {
            "Good. Clear the bags first, then we can stay out longer.",
            "That makes sense. More space means less stopping when things get good.",
            "Practical. I like it when preparation actually buys us time.",
        })

    def test_azele_fallback_understands_bag_slots_as_item_space(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "well we're going to get 5 more bag slots. that huge!",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertRegex(decision.response.lower(), r"slots|space|stay out|upgrade")
        self.assertNotRegex(decision.response.lower(), r"looking good|outfit|skirt")

    def test_azele_fallback_understands_small_equipment_pack_limit(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "i had no idea the bag, a Small Equipment Pack, only stores weapons and armor",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertRegex(decision.response.lower(), r"weapons|armor|gear-only|storage|room")
        self.assertNotRegex(decision.response.lower(), r"quick swaps|what kind of stuff")

    def test_model_reply_rejects_bag_slot_intent_miss(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "well we're going to get 5 more bag slots. that huge!",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("If it is an upgrade, take it. Looking good and staying alive can both happen.", event)

    def test_azele_fallback_handles_nicholas_sandford_grounded(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "only the items that Nicholas Sandford needs",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertRegex(decision.response, r"Nicholas|daily|trophy|Gift of the Huntsman")
        self.assertNotRegex(decision.response.lower(), r"weapons mostly|save potions")

    def test_model_reply_rejects_invented_nicholas_sandford_request(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "only the items that Nicholas Sandford needs",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        with self.assertRaisesRegex(ValueError, "unsupported Nicholas Sandford request"):
            validate_model_reply("Nicholas Sandford needs weapons mostly; save potions until later.", event)

    def test_model_reply_rejects_self_duplicate_reference(self) -> None:
        event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "snapshot",
                "metadata": {
                    "event_type": "snapshot",
                    "persona": "Azele",
                    "map_id": 162,
                    "map_name": "Regent Valley",
                    "close_hostile_count": 0,
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "unsupported self duplicate reference"):
            validate_model_reply("Five extra bags is nice, but someone else looking like me might take them.", event)

    def test_azele_fallback_handles_lead_the_way_continuation(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "lead the way then",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertIn(decision.response, {
            "Gladly. Stay close and try to look like this was your idea.",
            "Alright. I’ll set the pace, you keep up.",
            "Fine by me. Watch the edges while I pick the road.",
        })

    def test_azele_fallback_handles_red_iris_stage_continuation(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "yep. there's one around her stage",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertRegex(decision.response.lower(), r"althea|stage|iris")
        self.assertNotIn("what are we doing", decision.response.lower())

    def test_azele_fallback_understands_red_iris_for_bag_space(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "lets try to find 1 more red iris flower so we can get another bag. that would be a huge upgrade",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertRegex(decision.response.lower(), r"iris|flower|bag|space|slots")
        self.assertNotRegex(decision.response.lower(), r"looking good|staying alive")

    def test_azele_fallback_answers_six_gods_attunement_question(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "of all the 6 gods, which one do you feel the most attuned to?",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertIn("lyssa", decision.response.lower())
        self.assertNotIn("one more detail", decision.response.lower())

    def test_azele_fallback_reacts_to_black_dye_with_likely_source(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Loot",
                    "channel": "system",
                    "message": "Item dropped: Black Dye, likely from Charr Axe Fiend.",
                    "metadata": {
                        "event_type": "item_drop",
                        "persona": "Azele",
                        "agent_id": 99,
                        "agent_name": "Charr Axe Fiend",
                    },
                }
            )
        )

        self.assertIn("Black Dye", decision.response)
        self.assertIn("pre-Searing", decision.response)
        self.assertIn("Looked like", decision.response)
        self.assertIn("Charr Axe Fiend", decision.response)

    def test_azele_fallback_does_not_invent_black_dye_source(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Loot",
                    "channel": "system",
                    "message": "Item dropped: Black Dye.",
                    "metadata": {"event_type": "item_drop", "persona": "Azele"},
                }
            )
        )

        self.assertIn("Black Dye", decision.response)
        self.assertIn("did not see what dropped it", decision.response)

    def test_azele_fallback_reacts_to_purple_drop_with_likely_source(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Loot",
                    "channel": "system",
                    "message": "Item dropped: Purple rarity item, likely from Charr Axe Fiend.",
                    "metadata": {
                        "event_type": "item_drop",
                        "persona": "Azele",
                        "agent_id": 99,
                        "agent_name": "Charr Axe Fiend",
                    },
                }
            )
        )

        self.assertIn("Purple", decision.response)
        self.assertIn("worth a look", decision.response)
        self.assertIn("Charr Axe Fiend", decision.response)

    def test_azele_fallback_prompts_for_unknown_purple_drop(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Loot",
                    "channel": "system",
                    "message": "Item dropped: Purple rarity item.",
                    "metadata": {"event_type": "item_drop", "persona": "Azele"},
                }
            )
        )

        self.assertIn("Purple", decision.response)
        self.assertIn("What did it roll", decision.response)

    def test_azele_rejects_overly_mature_old_voice(self) -> None:
        self.assertRegex("Very undignified of me, tragically.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Obviously. I have a whole vibe to protect.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Praise accepted. Keep it cute.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("The Northlands is no place for peace-talkers.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Lead me on; let's get those irises.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Let's get those irises before they move away from us.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Keep your shield ready when we hit that line again.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Hey, glad you're back with me before things get messy again. Ready?", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex(
            "But right now, that doesn't matter as much as keeping us safe on these roads.",
            LOW_QUALITY_REPLY_PATTERNS,
        )
        self.assertRegex(
            "Ready to settle down and wait it out until you need us more than me waiting around?",
            LOW_QUALITY_REPLY_PATTERNS,
        )

    def test_rejects_old_english_theatre_voice(self) -> None:
        self.assertRegex("Aye, we shall take the road lest the Charr find us.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Keep thy bow ready upon this road.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("By my honour, mine arrow shall answer.", LOW_QUALITY_REPLY_PATTERNS)
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "ready?",
                "metadata": {"event_type": "player_chat", "persona": "Meliora Andru"},
            }
        )
        with self.assertRaisesRegex(ValueError, "low quality"):
            validate_model_reply("Aye, we shall take the road lest the Charr find us.", event)

    def test_azele_allows_natural_filler_openers_with_substance(self) -> None:
        self.assertRegex("Mm.", FILLER_ONLY_REPLY_PATTERN)
        self.assertRegex("hm, okay.", FILLER_ONLY_REPLY_PATTERN)
        self.assertNotRegex("Mm, cute. You say that like you want me to take the lead.", FILLER_ONLY_REPLY_PATTERN)
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "more like, we'll be doing the hunting, thank you very much",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        self.assertEqual(
            validate_model_reply("Mm, cute. You say that like you want me to take the lead.", event),
            "Mm, cute. You say that like you want me to take the lead.",
        )

    def test_model_reply_shape_rejects_runons_and_dangling_splits(self) -> None:
        self.assertTrue(model_reply_has_bad_shape("I"))
        self.assertTrue(model_reply_has_bad_shape("Yeah"))
        self.assertTrue(model_reply_has_bad_shape("I can handle whatever comes up from them later on if needed too since they're not worth"))
        self.assertTrue(
            model_reply_has_bad_shape(
                "My folks named it after their harvest festival; simple enough until Prince Rurik showed up in Ascalon City then things"
            )
        )
        self.assertTrue(
            model_reply_has_bad_shape(
                "I know you do like that outfit though it does fit well on me today isn't it nice hearing from someone who knows exactly why they look good here"
            )
        )
        self.assertTrue(model_reply_has_bad_shape("Feels good being home after all that travel though it looks different but"))
        self.assertTrue(
            model_reply_has_bad_shape(
                "A melandru stalker sounds better for her though, maybe she'll actually use that instead of just hoarding them in the pack while we circle every single path twice and pretend that is a real plan anyway."
            )
        )
        self.assertTrue(
            model_reply_has_bad_shape(
                "Yeah, let's go grab some gear and hit them hard at Piken Square first thing tomorrow morning while we're still fresh on Ascalon soil. Your call where exactly though; north of the"
            )
        )
        self.assertTrue(
            model_reply_has_bad_shape(
                "Fair enough, but don't get cocky yet. I've got a lot more power than most folks realize. Let us go find those Charr instead. Tell me where their lair is so we"
            )
        )
        self.assertFalse(model_reply_has_bad_shape("I know. Still nice to hear."))
        self.assertFalse(
            model_reply_has_bad_shape(
                "A melandru stalker sounds better for her though. Maybe she'll actually use that instead of just hoarding it."
            )
        )
        self.assertFalse(
            model_reply_has_bad_shape(
                "Morning! Just needed some fresh air after all those tunnels earlier anyway... ready for whatever comes next though."
            )
        )

    def test_meliora_name_origin_fallback_answers_directly(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "so where did you get the name Meliora",
                "metadata": {"event_type": "player_chat", "persona": "Meliora Andru"},
            }
        )

        decision = fallback_rule_decision(event)

        self.assertIn("mother", decision.response.lower())
        self.assertIn("ashford", decision.response.lower())
        self.assertIn("foible", decision.response.lower())
        self.assertIn("regent valley", decision.response.lower())
        self.assertNotIn("keep watch", decision.response.lower())
        self.assertNotIn("rurik", decision.response.lower())

    def test_meliora_allows_personal_ashford_references(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "so where did you get the name Meliora",
                "metadata": {"event_type": "player_chat", "persona": "Meliora Andru"},
            }
        )
        reply = (
            "My mother chose Meliora because it meant 'better things' to her. "
            "Andru is my Ashford family name."
        )

        self.assertEqual(validate_model_reply(reply, event), reply)

    def test_model_reply_allows_more_fluid_banter_and_punctuation(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "that was actually kind of fun",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        reply = "It was! Annoying, loud, and a little stupid, but yes. I had fun with you."

        self.assertEqual(validate_model_reply(reply, event), reply)

    def test_model_reply_shape_allows_longer_complete_conversational_replies(self) -> None:
        reply = (
            "I can tell you more, yes. I was born close enough to Ascalon City that the walls felt normal to me. "
            "My mother worried too much, my father pretended he did not, and I learned early that looking harmless "
            "made people underestimate me. That helped more than it probably should have. If you want the prettier "
            "version, I can give you that too, but the honest one is messier. I liked ribbons, sharp knives, and "
            "pretending I was not scared when the older trainees talked too loudly. I still do that sometimes. "
            "The pretending, I mean. Not the ribbons. Well, maybe the ribbons too."
        )

        self.assertFalse(model_reply_has_bad_shape(reply))
        self.assertGreater(len(hermes_daemon.split_gw_chat_lines(reply)), 5)
        self.assertLessEqual(len(hermes_daemon.split_gw_chat_lines(reply)), hermes_daemon.MAX_GW_REPLY_LINES)

    def test_voice_preference_context_stays_on_voice(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hey. i just gave you a new voice. instead of Bella, it's now Heart. you like the new voice?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        self.assertFalse(
            misses_clear_player_intent("Heart suits me better than Bella. Softer, but still mine.", event)
        )
        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("Charr are still out there, so we should hunt them after this.", event)

        decision = fallback_rule_decision(event)
        self.assertRegex(decision.response.lower(), r"heart|bella|voice|sound")
        self.assertNotRegex(decision.response.lower(), r"charr|hunt|combat")

    def test_voice_preference_rejects_overlong_tangent(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "do you like the Heart voice better than Bella?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        reply = (
            "Heart is warmer than Bella. I like it. "
            "Just do not expect me to be soft now. "
            "Charr are still out there. "
            "Maybe we should hunt them after this."
        )

        with self.assertRaisesRegex(ValueError, "misdirected voice reply"):
            validate_model_reply(reply, event)

    def test_model_reply_repairs_minor_checkin_grammar_before_validation(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hey. im feeling good. how are you?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        repaired = repair_model_reply(
            "Hey myself too. Feeling pretty good actually since you're up and about. What've got your spirits high today, exactly?"
        )

        self.assertIn("Me too", repaired)
        self.assertIn("What's got", repaired)
        self.assertEqual(validate_model_reply(repaired, event), repaired)

    def test_devona_pet_plan_avoids_generic_fallback(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "alright. im logging off, but our next quest is to get Devona a ranger pet that she can use.",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        decision = fallback_rule_decision(event)

        self.assertIn("Devona", decision.response)
        self.assertRegex(decision.response, r"\b(?:pet|stalker|ranger)\b")
        with self.assertRaisesRegex(ValueError, "missed clear player intent"):
            validate_model_reply("Yeah, I’m here. What are we doing?", event)

    def test_devona_pet_choice_gets_direct_recommendation(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "im thinking a melandru stalker or a warthog. what do you think?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        recent_reply_texts.append("Our next quest is to get Devona a ranger pet.")
        decision = fallback_rule_decision(event)

        self.assertRegex(decision.response, r"\b(?:stalker|warthog|Devona)\b")

    def test_map_entry_rejects_malformed_three_line_generation(self) -> None:
        event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "map_loaded",
                "metadata": {
                    "event_type": "map_loaded",
                    "persona": "Azele",
                    "map_id": 148,
                    "map_name": "Ascalon City",
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "low quality"):
            validate_model_reply(
                "Welcome back home then. "
                "It always smells like fresh green grass up here in Ascalon City compared to where I was last time. "
                "Ready to settle down and wait it out until you need us more than me waiting around?",
                event,
            )
        self.assertEqual(
            validate_model_reply(
                "Welcome back home then. "
                "It always smells like fresh green grass up here in Ascalon City. "
                "I could stay a while if you wanted to look around.",
                event,
            ),
            "Welcome back home then. "
            "It always smells like fresh green grass up here in Ascalon City. "
            "I could stay a while if you wanted to look around.",
        )

    def test_ollama_generation_includes_player_and_ambient_but_not_map_events(self) -> None:
        chat_event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "where is Piken Square?",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )
        map_event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "map_loaded",
                "metadata": {"event_type": "map_loaded", "persona": "Azele", "map_id": 146},
            }
        )
        snapshot_event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "snapshot",
                "metadata": {"event_type": "snapshot", "persona": "Azele", "map_id": 148, "map_name": "Ascalon City"},
            }
        )

        self.assertTrue(should_use_ollama_for_event(chat_event))
        self.assertFalse(should_use_ollama_for_event(map_event))
        self.assertFalse(should_use_ollama_for_event(snapshot_event))

        original_settings = hermes_daemon.settings
        try:
            hermes_daemon.settings = replace(original_settings, hermes_ambient_use_ollama=True)
            self.assertTrue(should_use_ollama_for_event(snapshot_event))
        finally:
            hermes_daemon.settings = original_settings

    def test_map_entry_with_ollama_enabled_uses_fast_local_comment(self) -> None:
        original_decide = hermes_daemon.decide_with_ollama
        hermes_daemon.decide_with_ollama = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected ollama call"))
        try:
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "System",
                        "channel": "system",
                        "message": "map_loaded",
                        "metadata": {
                            "event_type": "map_loaded",
                            "persona": "Azele",
                            "session_id": "map-fast-comment",
                            "map_id": 148,
                            "map_name": "Ascalon City",
                        },
                    }
                ),
                use_ollama=True,
            )
        finally:
            hermes_daemon.decide_with_ollama = original_decide

        self.assertEqual(len(replies), 1)
        self.assertIn("Ascalon City", replies[0].message)

    def test_ollama_generation_includes_item_drop_events(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Loot",
                "channel": "system",
                "message": "Item dropped: Black Dye, likely from Charr Axe Fiend.",
                "metadata": {
                    "event_type": "item_drop",
                    "persona": "Azele",
                    "agent_id": 99,
                    "agent_name": "Charr Axe Fiend",
                },
            }
        )

        self.assertTrue(should_use_ollama_for_event(event))

    def test_prompt_includes_item_drop_source_as_likely(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Loot",
                "channel": "system",
                "message": "Item dropped: Black Dye, likely from Charr Axe Fiend.",
                "metadata": {
                    "event_type": "item_drop",
                    "persona": "Azele",
                    "agent_id": 99,
                    "agent_name": "Charr Axe Fiend",
                },
            }
        )

        prompt = build_character_reply_prompt(event)

        self.assertIn("React to a notable item drop", prompt)
        self.assertIn("Likely drop source: Charr Axe Fiend", prompt)
        self.assertIn("treat it as likely/inferred rather than certain", prompt)
        self.assertIn("Black Dye is extremely exciting in pre-Searing Ascalon", prompt)
        self.assertIn("Purple, Gold, and Green rarity drops are also unusual enough to notice", prompt)

    def test_polling_skips_records_from_before_daemon_start(self) -> None:
        older = hermes_daemon.DAEMON_STARTED_AT - timedelta(minutes=5)
        fresh = datetime.now(timezone.utc)

        self.assertTrue(is_stale_polled_record({"created_at": older.isoformat()}))
        self.assertFalse(is_stale_polled_record({"created_at": fresh.isoformat()}))

    def test_azele_fallback_uses_realistic_casual_voice(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "of course you are",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertEqual(decision.response, "Yeah. You know me. Keep up.")

    def test_azele_fallback_handles_locked_and_loaded_tease(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "you had to locked and loaded didn't you",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertRegex(decision.response.lower(), r"ready|opening|walked|dare")
        self.assertNotIn("okay. keep going", decision.response.lower())

    def test_azele_fallback_does_not_misread_you_as_yo(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "you look good",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertEqual(decision.response, "I know. Still nice to hear.")

    def test_azele_fallback_avoids_repeating_recent_greeting(self) -> None:
        recent_reply_texts.append("Hey. I’m here.")

        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "hey",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertNotEqual(decision.response, "Hey. I’m here.")

    def test_azele_repeat_fallback_does_not_say_resetting(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "you are repeating yourself",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertNotIn("resetting", decision.response.lower())
        self.assertNotIn("new line", decision.response.lower())
        self.assertRegex(decision.response.lower(), r"loop|stuck|heard|properly|with you")

    def test_azele_fallback_answers_dwarven_ale_consumable_roleplay(self) -> None:
        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "i just made you drink 15 dwarven ale. hows it feel?",
                    "metadata": {"event_type": "player_chat", "persona": "Azele"},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertRegex(decision.response.lower(), r"ale|fifteen|15|warm|city|stairs")
        self.assertNotIn("give me one more detail", decision.response.lower())

    def test_player_chat_fast_dwarven_ale_reply_skips_ollama_wait(self) -> None:
        original = hermes_daemon.decide_with_ollama

        def fail_if_called(event: hermes_daemon.TelemetryEvent) -> hermes_daemon.HermesDecision:
            raise AssertionError("Dwarven Ale fast reply should not wait on Ollama")

        hermes_daemon.decide_with_ollama = fail_if_called
        try:
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "Player",
                        "channel": "party",
                        "message": "i just made you drink 15 dwarven ale. hows it feel?",
                        "metadata": {
                            "event_type": "player_chat",
                            "persona": "Azele",
                            "session_id": "ale-timeout",
                            "map_id": 148,
                            "map_name": "Ascalon City",
                        },
                    }
                ),
                record_id=815,
                use_ollama=True,
            )
        finally:
            hermes_daemon.decide_with_ollama = original

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].trigger_log_id, 815)
        self.assertRegex(replies[0].message.lower(), r"ale|fifteen|15|warm|city|stairs")
        self.assertGreater(world_state.last_player_chat_at, 0)

    def test_lightweight_party_chat_uses_ollama_before_fallback(self) -> None:
        original = hermes_daemon.decide_with_ollama
        seen_messages: list[str] = []

        def fake_decide(event: hermes_daemon.TelemetryEvent, **_: object) -> hermes_daemon.HermesDecision:
            seen_messages.append(event.message)
            generated = {
                "hi azele!": "Hey. I am here. What are we doing?",
                "gl": "Good luck to us, then. Stay close.",
                "gz": "Thank you. I am trying not to look too pleased.",
                "ty all": "Anytime. I can be useful when I want to be.",
            }[event.message]
            return hermes_daemon.HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="NORMAL",
                response=generated,
            )

        hermes_daemon.decide_with_ollama = fake_decide
        try:
            for message in ["hi azele!", "gl", "gz", "ty all"]:
                with self.subTest(message=message):
                    replies = process_event(
                        event_from_game_log(
                            {
                                "sender": "Player",
                                "channel": "party",
                                "message": message,
                                "metadata": {
                                    "event_type": "player_chat",
                                    "persona": "Azele",
                                    "session_id": f"lightweight-{message}",
                                    "map_id": 148,
                                    "map_name": "Ascalon City",
                                },
                            }
                        ),
                        record_id=900,
                        use_ollama=True,
                    )
                    self.assertEqual(len(replies), 1)
                    self.assertNotIn("Generated reply", replies[0].message)
        finally:
            hermes_daemon.decide_with_ollama = original
        self.assertEqual(seen_messages, ["hi azele!", "gl", "gz", "ty all"])

    def test_player_chat_ollama_uses_short_latency_budget(self) -> None:
        original_settings = hermes_daemon.settings
        original_generate = hermes_daemon.ollama_generate_visible
        observed: list[float | None] = []
        observed_predict: list[int | None] = []

        def fake_generate(
            prompt: str,
            *,
            timeout_seconds: float | None = None,
            num_predict: int | None = None,
        ) -> str:
            observed.append(timeout_seconds)
            observed_predict.append(num_predict)
            return "I know. Still nice to hear."

        try:
            hermes_daemon.settings = replace(
                original_settings,
                hermes_player_chat_ollama_timeout_seconds=8.0,
                hermes_player_chat_ollama_num_predict=160,
            )
            hermes_daemon.ollama_generate_visible = fake_generate

            decision = hermes_daemon.decide_with_ollama(
                event_from_game_log(
                    {
                        "sender": "Player",
                        "channel": "party",
                        "message": "do you like the new armor?",
                        "metadata": {"event_type": "player_chat", "persona": "Azele"},
                    }
                )
            )
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.ollama_generate_visible = original_generate

        self.assertEqual(decision.response, "I know. Still nice to hear.")
        self.assertEqual(observed, [8.0])
        self.assertEqual(observed_predict, [160])

    def test_player_chat_ollama_timeout_falls_back_quickly(self) -> None:
        original = hermes_daemon.decide_with_ollama

        def timeout(_event: hermes_daemon.TelemetryEvent) -> hermes_daemon.HermesDecision:
            raise TimeoutError("timed out")

        hermes_daemon.decide_with_ollama = timeout
        try:
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "Player",
                        "channel": "party",
                        "message": "hey azele, are you there?",
                        "metadata": {
                            "event_type": "player_chat",
                            "persona": "Azele",
                            "session_id": "chat-timeout",
                            "map_id": 148,
                            "map_name": "Ascalon City",
                        },
                    }
                ),
                record_id=816,
                use_ollama=True,
            )
        finally:
            hermes_daemon.decide_with_ollama = original

        self.assertEqual(len(replies), 1)
        self.assertNotIn("one more detail", replies[0].message.lower())
        self.assertTrue(replies[0].message)

    def test_stale_direct_chat_reply_is_discarded_after_newer_player_chat(self) -> None:
        original = hermes_daemon.decide_with_ollama

        def slow_reply(_event: hermes_daemon.TelemetryEvent, *, record_id: int | None = None) -> hermes_daemon.HermesDecision:
            with hermes_daemon.world_state_lock:
                hermes_daemon.world_state.last_player_chat_at = time.time()
            return hermes_daemon.HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="NORMAL",
                response="Old answer that should not arrive late.",
            )

        hermes_daemon.decide_with_ollama = slow_reply
        try:
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "Player",
                        "channel": "party",
                        "message": "first version of the question",
                        "metadata": {"event_type": "player_chat", "persona": "Azele", "session_id": "stale-direct"},
                    }
                ),
                record_id=901,
                use_ollama=True,
            )
        finally:
            hermes_daemon.decide_with_ollama = original

        self.assertEqual(replies, [])

    def test_unknown_quest_change_is_silent(self) -> None:
        replies = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "active_quest_changed",
                    "metadata": {
                        "event_type": "active_quest_changed",
                        "persona": "Azele",
                        "active_quest_name": "脂瀙\ue493뉃⇄",
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(replies, [])

    def test_known_presearing_map_id_gets_arrival_comment_without_map_name(self) -> None:
        replies = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "map_loaded",
                    "metadata": {
                        "event_type": "map_loaded",
                        "persona": "Azele",
                        "session_id": "map-test",
                        "map_id": 165,
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual([reply.message for reply in replies], ["Foible's Fair. Small, but I know it."])

    def test_map_arrival_comments_rotate_by_map(self) -> None:
        first = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "map_loaded",
                    "metadata": {
                        "event_type": "map_loaded",
                        "persona": "Azele",
                        "session_id": "map-rotate-a",
                        "map_id": 148,
                    },
                }
            ),
            use_ollama=False,
        )
        last_map_comment_by_session.clear()
        world_state.last_spoken_at = 0
        second = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "map_loaded",
                    "metadata": {
                        "event_type": "map_loaded",
                        "persona": "Azele",
                        "session_id": "map-rotate-a",
                        "map_id": 148,
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0].message, second[0].message)

    def test_transition_start_map_change_does_not_quip(self) -> None:
        replies = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "map_change",
                    "metadata": {
                        "event_type": "map_change",
                        "persona": "Azele",
                        "session_id": "map-test",
                        "map_id": 165,
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(replies, [])

    def test_map_changed_snapshot_does_not_make_arrival_quip(self) -> None:
        replies = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "map_changed",
                    "metadata": {
                        "event_type": "map_changed",
                        "persona": "Azele",
                        "session_id": "map-test",
                        "map_id": 148,
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(replies, [])

    def test_npc_dialogue_can_trigger_azele_aside(self) -> None:
        replies = process_event(
            event_from_game_log(
                {
                    "sender": "Game",
                    "channel": "local",
                    "message": "The Charr have been seen near the Wall.",
                    "payload": {
                        "event_type": "chat_log",
                        "persona": "Azele",
                        "session_id": "npc-dialogue",
                        "map_id": 148,
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual([reply.message for reply in replies], ["See? Not just me being dramatic. We should be ready."])

    def test_overhead_npc_speech_bubble_can_trigger_azele_aside(self) -> None:
        replies = process_event(
            event_from_game_log(
                {
                    "sender": "Agent 123",
                    "channel": "local",
                    "message": "Please, someone help us before more Charr come.",
                    "payload": {
                        "event_type": "npc_speech_bubble",
                        "persona": "Azele",
                        "session_id": "npc-bubble",
                        "map_id": 148,
                        "agent_id": 123,
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual([reply.message for reply in replies], ["See? Not just me being dramatic. We should be ready."])

    def test_memory_flush_threshold_is_more_frequent(self) -> None:
        events = deque(
            {
                "event_type": "player_chat",
                "message": f"line {index}",
            }
            for index in range(6)
        )

        reason = should_flush_memory_buffer(events, events[-1], last_write_at=time.time() - 40.0)

        self.assertEqual(reason, "event_threshold")

    def test_ordinary_player_chat_does_not_become_memory(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "hello",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        self.assertIsNone(memory_event_from(event, record_id=1))

    def test_explicit_player_memory_becomes_relationship_note(self) -> None:
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "remember that I like checking every hidden stash",
                "metadata": {"event_type": "player_chat", "persona": "Azele"},
            }
        )

        memory_event = memory_event_from(event, record_id=2)

        self.assertIsNotNone(memory_event)
        assert memory_event is not None
        self.assertEqual(memory_event["notability"], "durable_player_note")
        reason = should_flush_memory_buffer(deque([memory_event]), memory_event, last_write_at=time.time())
        self.assertEqual(reason, "durable_player_note")

    def test_missing_persona_docs_are_optional_local_files(self) -> None:
        self.assertEqual(persona_living_notes("Persona Without Local Docs"), "")

    def test_persona_living_notes_loads_optional_local_lore_doc(self) -> None:
        path = hermes_daemon.PERSONA_MEMORY_DIR / "test-persona-local-docs.lore.md"
        try:
            path.write_text(
                "# Test Persona world memory notes\n\n"
                "- This is local lore that should not need to be committed.\n",
                encoding="utf-8",
            )
            notes = persona_living_notes("Test Persona Local Docs")
        finally:
            path.unlink(missing_ok=True)

        self.assertIn("World memory notes", notes)
        self.assertIn("local lore that should not need to be committed", notes)

    def test_local_persona_notes_filter_unsafe_or_noisy_legacy_lines(self) -> None:
        notes = sanitize_local_persona_notes_for_prompt(
            "# Local notes\n\n"
            "- Azele grew up around Ascalon City and remembers ordinary training nerves.\n"
            "- A malformed old line says fifteen years old and contains graphic traumatic wording.\n",
            max_chars=500,
        )

        self.assertIn("ordinary training nerves", notes)
        self.assertNotIn("fifteen years old", notes)
        self.assertNotIn("graphic traumatic wording", notes)

    def test_azele_backstory_prompt_prioritizes_lived_memory(self) -> None:
        base = hermes_daemon.PERSONA_MEMORY_DIR / "prompt-memory-persona"
        paths = [
            base.with_suffix(".md"),
            hermes_daemon.PERSONA_MEMORY_DIR / "prompt-memory-persona.memory.md",
        ]
        try:
            paths[0].write_text(
                "- Prompt Memory Persona grew up around Ascalon City and remembers ordinary training nerves.\n",
                encoding="utf-8",
            )
            paths[1].write_text(
                "- Prompt Memory Persona lost a serious Survivor run at level 15 and still feels raw about Charr ambushes.\n"
                "- A malformed old line says fifteen years old and contains graphic traumatic wording.\n",
                encoding="utf-8",
            )
            event = event_from_game_log(
                {
                    "sender": "Player",
                    "channel": "party",
                    "message": "do you remember anything from your past?",
                    "metadata": {"event_type": "player_chat", "persona": "Prompt Memory Persona"},
                }
            )

            prompt = build_character_reply_prompt(event)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

        self.assertIn("answer with lived personal history first", prompt)
        self.assertIn("Living character notes", prompt)
        self.assertIn("Personal memory notes", prompt)
        self.assertIn("ordinary training nerves", prompt)
        self.assertIn("lost a serious Survivor run", prompt)
        self.assertNotIn("fifteen years old", prompt.lower())
        self.assertNotIn("graphic traumatic wording", prompt.lower())

    def test_prompt_memory_filter_skips_noisy_legacy_session_summaries(self) -> None:
        memories = [
            hermes_daemon.MemoryRow(
                character_name="Azele",
                memory_type="session_summary",
                title="Azele session: map movement, quest progress",
                summary_text="Azele moved through Ascalon City, The Northlands.",
                metadata={"source": "hermes_memory_writer"},
            ),
            hermes_daemon.MemoryRow(
                character_name="Azele",
                memory_type="relationship_note",
                title="Azele session: relationship note",
                summary_text='The player told Azele: "remember that I like checking every hidden stash".',
                tags=["relationship", "player_preference"],
                metadata={"notability": ["durable_player_note"], "source": "hermes_memory_writer"},
            ),
            hermes_daemon.MemoryRow(
                character_name="Azele",
                memory_type="session_summary",
                title="Azele session: play notes",
                summary_text="Azele continued the session.",
                metadata={},
            ),
        ]

        filtered = prompt_relevant_memories(memories)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].memory_type, "relationship_note")

    def test_snapshot_can_trigger_low_frequency_ambient_quip(self) -> None:
        replies = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "snapshot",
                    "metadata": {
                        "event_type": "snapshot",
                        "persona": "Azele",
                        "session_id": "ambient-test",
                        "map_id": 148,
                        "map_name": "Ascalon City",
                        "close_hostile_count": 0,
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(len(replies), 1)
        self.assertRegex(replies[0].message.lower(), r"city|ascalon|streets|traders|guards|hair")

    def test_map_entry_uses_local_comment_even_when_ollama_enabled(self) -> None:
        original = hermes_daemon.character_reply_with_ollama
        hermes_daemon.character_reply_with_ollama = lambda event, **_: (_ for _ in ()).throw(AssertionError("unexpected ollama call"))
        try:
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "System",
                        "channel": "system",
                        "message": "map_loaded",
                        "metadata": {
                            "event_type": "map_loaded",
                            "persona": "Azele",
                            "session_id": "map-llm-test",
                            "map_id": 148,
                            "map_name": "Ascalon City",
                        },
                    }
                ),
                use_ollama=True,
            )
        finally:
            hermes_daemon.character_reply_with_ollama = original

        self.assertEqual(len(replies), 1)
        self.assertIn("Ascalon City", replies[0].message)

    def test_map_entry_has_no_stale_generation_window_when_ollama_enabled(self) -> None:
        original = hermes_daemon.character_reply_with_ollama

        def generate_after_player_chat(event: hermes_daemon.TelemetryEvent, **_: object) -> hermes_daemon.HermesDecision:
            world_state.last_player_chat_at = time.time()
            return hermes_daemon.HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="LOW",
                response="Generated map thought. Where first?",
            )

        hermes_daemon.character_reply_with_ollama = generate_after_player_chat
        try:
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "System",
                        "channel": "system",
                        "message": "map_loaded",
                        "metadata": {
                            "event_type": "map_loaded",
                            "persona": "Azele",
                            "session_id": "map-stale-test",
                            "map_id": 148,
                            "map_name": "Ascalon City",
                        },
                    }
                ),
                use_ollama=True,
            )
        finally:
            hermes_daemon.character_reply_with_ollama = original

        self.assertEqual(len(replies), 1)
        self.assertIn("Ascalon City", replies[0].message)

    def test_ambient_snapshot_uses_ollama_generation_when_enabled(self) -> None:
        original = hermes_daemon.character_reply_with_ollama
        original_settings = hermes_daemon.settings
        hermes_daemon.character_reply_with_ollama = lambda event, **_: hermes_daemon.HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response="Generated quiet moment. What are you watching?",
        )
        try:
            hermes_daemon.settings = replace(original_settings, hermes_ambient_use_ollama=True)
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "System",
                        "channel": "system",
                        "message": "snapshot",
                        "metadata": {
                            "event_type": "snapshot",
                            "persona": "Azele",
                            "session_id": "ambient-llm-test",
                            "map_id": 148,
                            "map_name": "Ascalon City",
                            "close_hostile_count": 0,
                        },
                    }
                ),
                use_ollama=True,
            )
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.character_reply_with_ollama = original

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].message, "Generated quiet moment. What are you watching?")

    def test_ambient_snapshot_uses_local_quip_when_ambient_ollama_disabled(self) -> None:
        original = hermes_daemon.character_reply_with_ollama
        original_settings = hermes_daemon.settings

        def fail_if_called(event: object, **_: object) -> hermes_daemon.HermesDecision:
            raise AssertionError("ambient snapshot should not call Ollama when ambient Ollama is disabled")

        hermes_daemon.character_reply_with_ollama = fail_if_called
        try:
            hermes_daemon.settings = replace(original_settings, hermes_ambient_use_ollama=False)
            replies = process_event(
                event_from_game_log(
                    {
                        "sender": "System",
                        "channel": "system",
                        "message": "snapshot",
                        "metadata": {
                            "event_type": "snapshot",
                            "persona": "Azele",
                            "session_id": "ambient-local-test",
                            "map_id": 148,
                            "map_name": "Ascalon City",
                            "close_hostile_count": 0,
                        },
                    }
                ),
                use_ollama=True,
            )
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.character_reply_with_ollama = original

        self.assertEqual(len(replies), 1)
        self.assertNotEqual(replies[0].message, "Generated quiet moment. What are you watching?")

    def test_ambient_snapshot_rejects_invented_purple_loot_hook(self) -> None:
        event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "snapshot",
                "metadata": {
                    "event_type": "snapshot",
                    "persona": "Azele",
                    "session_id": "ambient-loot-test",
                    "map_id": 166,
                    "map_name": "Fort Ranik",
                    "close_hostile_count": 0,
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "unsupported ambient loot reference"):
            validate_model_reply(
                "Hey. That purple thing is worth stopping for—go check it while I keep watch over us both.",
                event,
            )

    def test_item_drop_allows_purple_loot_reference(self) -> None:
        original_settings = hermes_daemon.settings
        event = event_from_game_log(
            {
                "sender": "Loot",
                "channel": "system",
                "message": "Item dropped: Purple rarity item.",
                "metadata": {"event_type": "item_drop", "persona": "Azele"},
            }
        )

        try:
            hermes_daemon.settings = replace(original_settings, supabase_url="", supabase_service_key="")
            self.assertEqual(
                validate_model_reply("Purple out here? That is worth a look. What did it roll?", event),
                "Purple out here? That is worth a look. What did it roll?",
            )
        finally:
            hermes_daemon.settings = original_settings

    def test_snapshot_ambient_quip_respects_cooldown(self) -> None:
        event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "snapshot",
                "metadata": {
                    "event_type": "snapshot",
                    "persona": "Azele",
                    "session_id": "ambient-test",
                    "map_id": 148,
                    "map_name": "Ascalon City",
                },
            }
        )

        first = process_event(event, use_ollama=False)
        second = process_event(event, use_ollama=False)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_ambient_heartbeat_can_restore_periodic_quip_without_snapshot_row(self) -> None:
        now = time.time()
        world_state.persona = "Azele"
        world_state.session_id = "ambient-heartbeat"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now - (AMBIENT_HEARTBEAT_ACTIVITY_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        reply = ambient_heartbeat_reply(now=now)
        second = ambient_heartbeat_reply(now=now)

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertEqual(reply.persona, "Azele")
        self.assertEqual(reply.channel, "party")
        self.assertEqual(reply.urgency, "LOW")
        self.assertEqual(reply.metadata["trigger"], "ambient_heartbeat")
        self.assertIsNone(second)

    def test_ambient_heartbeat_uses_active_non_azele_persona(self) -> None:
        now = time.time()
        world_state.persona = "Meliora Andru"
        world_state.session_id = "ambient-heartbeat-meliora"
        world_state.map_id = 149
        world_state.map_name = "Regent Valley"
        world_state.last_interaction_timestamp = now - (AMBIENT_HEARTBEAT_ACTIVITY_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        reply = ambient_heartbeat_reply(now=now)

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertEqual(reply.persona, "Meliora Andru")
        self.assertEqual(reply.metadata["trigger"], "ambient_heartbeat")

    def test_ambient_heartbeat_waits_after_player_chat(self) -> None:
        now = time.time()
        world_state.persona = "Azele"
        world_state.session_id = "ambient-heartbeat-player-chat"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now
        world_state.last_player_chat_at = now - (hermes_daemon.AMBIENT_AFTER_PLAYER_CHAT_QUIET_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        self.assertIsNone(ambient_heartbeat_reply(now=now))

    def test_ambient_heartbeat_waits_after_recent_supabase_player_chat(self) -> None:
        now = time.time()
        world_state.persona = "Azele"
        world_state.session_id = "ambient-heartbeat-db-player-chat"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now - (AMBIENT_HEARTBEAT_ACTIVITY_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        original = hermes_daemon.recent_player_chat_in_supabase
        hermes_daemon.recent_player_chat_in_supabase = lambda persona, session_id, checked_at, quiet: True
        try:
            self.assertIsNone(ambient_heartbeat_reply(now=now))
        finally:
            hermes_daemon.recent_player_chat_in_supabase = original

    def test_recent_player_chat_query_uses_existing_game_log_columns(self) -> None:
        original_settings = hermes_daemon.settings
        original_create_client = hermes_daemon.create_supabase_client
        selected_columns: list[str] = []

        class FakeQuery:
            def select(self, columns: str) -> "FakeQuery":
                selected_columns.append(columns)
                return self

            def eq(self, *_args: object) -> "FakeQuery":
                return self

            def order(self, *_args: object, **_kwargs: object) -> "FakeQuery":
                return self

            def limit(self, *_args: object) -> "FakeQuery":
                return self

            def execute(self) -> object:
                created_at = datetime.now(timezone.utc).isoformat()

                class Response:
                    data = [
                        {
                            "id": 1,
                            "created_at": created_at,
                            "sender": "Player",
                            "channel": "party",
                            "payload": {
                                "event_type": "player_chat",
                                "persona": "Azele",
                                "session_id": "session-1",
                            },
                        }
                    ]

                return Response()

        class FakeClient:
            def table(self, table_name: str) -> FakeQuery:
                self.table_name = table_name
                return FakeQuery()

        try:
            hermes_daemon.settings = replace(
                original_settings,
                supabase_url="https://example.supabase.co",
                supabase_service_key="service-key",
            )
            hermes_daemon.create_supabase_client = lambda settings: FakeClient()

            result = hermes_daemon.recent_player_chat_in_supabase("Azele", "session-1", time.time(), 60)

            self.assertTrue(result)
            self.assertEqual(selected_columns, ["id,created_at,sender,channel,payload"])
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.create_supabase_client = original_create_client

    def test_pending_replies_are_flushed_before_player_chat(self) -> None:
        original_settings = hermes_daemon.settings
        original_create_client = hermes_daemon.create_supabase_client
        updated_ids: list[int] = []

        class FakeQuery:
            def __init__(self) -> None:
                self.is_update = False

            def select(self, *_args: object) -> "FakeQuery":
                return self

            def eq(self, *_args: object) -> "FakeQuery":
                return self

            def is_(self, *_args: object) -> "FakeQuery":
                return self

            def order(self, *_args: object, **_kwargs: object) -> "FakeQuery":
                return self

            def limit(self, *_args: object) -> "FakeQuery":
                return self

            def update(self, *_args: object) -> "FakeQuery":
                self.is_update = True
                return self

            def in_(self, _column: str, values: list[int]) -> "FakeQuery":
                updated_ids.extend(values)
                return self

            def execute(self) -> object:
                class Response:
                    data = [
                        {
                            "id": 41,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "payload": {"session_id": "session-1", "trigger_event_type": "map_loaded"},
                        },
                        {
                            "id": 42,
                            "created_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            "payload": {"session_id": "session-1", "trigger_event_type": "player_chat"},
                        }
                    ]

                return Response()

        class FakeClient:
            def table(self, _table_name: str) -> FakeQuery:
                return FakeQuery()

        try:
            hermes_daemon.settings = replace(
                original_settings,
                supabase_url="https://example.supabase.co",
                supabase_service_key="service-key",
            )
            hermes_daemon.create_supabase_client = lambda settings: FakeClient()

            expired = expire_pending_replies_before_player_chat("Azele", "session-1")

            self.assertEqual(expired, 2)
            self.assertEqual(updated_ids, [41, 42])
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.create_supabase_client = original_create_client

    def test_map_entry_silent_after_recent_player_chat(self) -> None:
        world_state.map_id = 147
        world_state.map_name = "The Northlands"
        world_state.last_player_chat_at = time.time()

        replies = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "map_loaded",
                    "metadata": {
                        "event_type": "map_loaded",
                        "persona": "Azele",
                        "session_id": "recent-chat-map",
                        "map_id": 147,
                        "map_name": "The Northlands",
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(replies, [])

    def test_map_entry_speaks_after_recent_player_chat_when_map_changed(self) -> None:
        world_state.persona = "Azele"
        world_state.session_id = "recent-chat-map-transition"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_player_chat_at = time.time()
        world_state.last_spoken_at = time.time()

        replies = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "map_loaded",
                    "metadata": {
                        "event_type": "map_loaded",
                        "persona": "Azele",
                        "session_id": "recent-chat-map-transition",
                        "map_id": 779,
                        "map_name": "Piken Square",
                    },
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(len(replies), 1)
        self.assertRegex(replies[0].message.lower(), r"piken|bearings|new ground")

    def test_ambient_heartbeat_uses_ollama_when_enabled(self) -> None:
        now = time.time()
        world_state.persona = "Azele"
        world_state.session_id = "ambient-heartbeat-llm"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now - (AMBIENT_HEARTBEAT_ACTIVITY_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        original = hermes_daemon.character_reply_with_ollama
        original_settings = hermes_daemon.settings
        hermes_daemon.character_reply_with_ollama = lambda event, **kwargs: hermes_daemon.HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response="City is calm for once. Want to use the quiet or move?",
        )
        try:
            hermes_daemon.settings = replace(original_settings, hermes_ambient_use_ollama=True)
            reply = ambient_heartbeat_reply(now=now, use_ollama=True)
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.character_reply_with_ollama = original

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertEqual(reply.message, "City is calm for once. Want to use the quiet or move?")
        self.assertEqual(reply.metadata["trigger"], "ambient_heartbeat")

    def test_ambient_heartbeat_uses_short_ollama_timeout(self) -> None:
        now = time.time()
        world_state.persona = "Azele"
        world_state.session_id = "ambient-heartbeat-llm-timeout"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now - (AMBIENT_HEARTBEAT_ACTIVITY_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        captured: dict[str, object] = {}
        original = hermes_daemon.character_reply_with_ollama
        original_settings = hermes_daemon.settings

        def capture_options(event: object, **kwargs: object) -> hermes_daemon.HermesDecision:
            captured.update(kwargs)
            return hermes_daemon.HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="LOW",
                response="City is calm for once. Want to use the quiet or move?",
            )

        hermes_daemon.character_reply_with_ollama = capture_options
        try:
            hermes_daemon.settings = replace(original_settings, hermes_ambient_use_ollama=True)
            reply = ambient_heartbeat_reply(now=now, use_ollama=True)
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.character_reply_with_ollama = original

        self.assertIsNotNone(reply)
        self.assertEqual(captured["timeout_seconds"], hermes_daemon.AMBIENT_OLLAMA_TIMEOUT_SECONDS)
        self.assertEqual(captured["num_predict"], hermes_daemon.AMBIENT_OLLAMA_NUM_PREDICT)

    def test_ambient_heartbeat_hides_internal_trigger_from_model_prompt(self) -> None:
        now = time.time()
        world_state.persona = "Azele"
        world_state.session_id = "ambient-heartbeat-prompt"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now - (AMBIENT_HEARTBEAT_ACTIVITY_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        captured: dict[str, object] = {}
        original = hermes_daemon.character_reply_with_ollama
        original_settings = hermes_daemon.settings

        def capture_event(event: object, **kwargs: object) -> hermes_daemon.HermesDecision:
            captured["event"] = event
            return hermes_daemon.HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="LOW",
                response="Ascalon City is calm enough to breathe. What are you watching for?",
            )

        hermes_daemon.character_reply_with_ollama = capture_event
        try:
            hermes_daemon.settings = replace(original_settings, hermes_ambient_use_ollama=True)
            reply = ambient_heartbeat_reply(now=now, use_ollama=True)
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.character_reply_with_ollama = original

        self.assertIsNotNone(reply)
        event = captured["event"]
        prompt = build_character_reply_prompt(event)  # type: ignore[arg-type]
        self.assertNotIn("ambient heartbeat", prompt.lower())
        self.assertIn("quiet ambient moment", prompt.lower())

    def test_ambient_model_reply_rejects_heartbeat_metaphor(self) -> None:
        event = event_from_game_log(
            {
                "sender": "System",
                "channel": "system",
                "message": "quiet ambient moment",
                "metadata": {
                    "event_type": "snapshot",
                    "persona": "Azele",
                    "map_id": 148,
                    "map_name": "Ascalon City",
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "ambient scheduler"):
            validate_model_reply("Yeah... that heartbeat feels closer now than last time.", event)

    def test_ambient_heartbeat_falls_back_to_local_quip_when_ollama_fails(self) -> None:
        now = time.time()
        world_state.persona = "Azele"
        world_state.session_id = "ambient-heartbeat-llm-fallback"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now - (AMBIENT_HEARTBEAT_ACTIVITY_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        original = hermes_daemon.character_reply_with_ollama
        original_settings = hermes_daemon.settings

        def fail_ollama(event: object, **kwargs: object) -> hermes_daemon.HermesDecision:
            raise RuntimeError("offline")

        hermes_daemon.character_reply_with_ollama = fail_ollama
        try:
            hermes_daemon.settings = replace(original_settings, hermes_ambient_use_ollama=True)
            reply = ambient_heartbeat_reply(now=now, use_ollama=True)
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.character_reply_with_ollama = original

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertNotEqual(reply.message, "Generated heartbeat line. Still with me?")
        self.assertEqual(reply.metadata["trigger"], "ambient_heartbeat")
        self.assertRegex(reply.message.lower(), r"city|ascalon|streets|traders|guards|hair")

    def test_ambient_heartbeat_ignores_unknown_persona(self) -> None:
        now = time.time()
        world_state.persona = "Unknown Character"
        world_state.session_id = "ambient-heartbeat"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        self.assertIsNone(ambient_heartbeat_reply(now=now))

    def test_ambient_identity_ignores_unknown_persona_before_pending_check(self) -> None:
        world_state.persona = "Unknown Character"
        world_state.session_id = "ambient-heartbeat"

        self.assertIsNone(ambient_identity())

        world_state.persona = "Azele"
        self.assertEqual(ambient_identity(), ("Azele", "ambient-heartbeat"))

    def test_parse_created_at_handles_supabase_timestamps(self) -> None:
        parsed = hermes_daemon._parse_created_at("2026-06-28T11:49:35.509257+00:00")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.tzinfo, timezone.utc)

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
                    "distance": 650,
                    "payload": {"hostile_count": 3, "close_hostile_count": 3, "closest_hostile_distance": 650},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "HIGH")

    def test_invisible_danger_spike_is_silent(self) -> None:
        replies = process_event(
            event_from_environment_alert(
                {
                    "alert_type": "danger_spike",
                    "severity": "HIGH",
                    "message": "3 hostile enemies are close.",
                    "distance": 1400,
                    "payload": {"hostile_count": 3, "close_hostile_count": 3, "closest_hostile_distance": 1400},
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(replies, [])

    def test_fallback_rule_replies_to_under_attack(self) -> None:
        decision = fallback_rule_decision(
            event_from_environment_alert(
                {
                    "alert_type": "under_attack",
                    "severity": "HIGH",
                    "message": "Azele is taking hits.",
                    "payload": {"player_hp": 0.42},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "HIGH")
        self.assertIn("42%", decision.response)
        self.assertTrue(any(word in decision.response.lower() for word in ["hit", "pain", "cover", "help"]))

    def test_fallback_rule_replies_to_status_effect(self) -> None:
        decision = fallback_rule_decision(
            event_from_environment_alert(
                {
                    "alert_type": "status_effect",
                    "severity": "HIGH",
                    "message": "Azele is dazed.",
                    "payload": {"effect_type": "condition", "effect_name": "dazed"},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "HIGH")
        self.assertIn("dazed", decision.response.lower())
        self.assertLessEqual(len(decision.response), 119)

    def test_fallback_rule_replies_to_combat_over(self) -> None:
        decision = fallback_rule_decision(
            event_from_environment_alert(
                {
                    "alert_type": "combat_over",
                    "severity": "LOW",
                    "message": "Combat ended.",
                    "payload": {"hostile_count": 0, "close_hostile_count": 0, "dead_hostile_count": 3},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "NORMAL")
        self.assertTrue(any(word in decision.response.lower() for word in ["down", "handled", "breathing"]))
        self.assertLessEqual(len(decision.response), 119)

    def test_short_ack_does_not_repeat_scourge_context_reply(self) -> None:
        recent_reply_texts.append("Yeah, another run. Devona wants Maz Scourgeheart stopped, and honestly so do I.")

        decision = fallback_rule_decision(
            event_from_game_log(
                {
                    "id": 1930,
                    "sender": "Player",
                    "channel": "party",
                    "message": "agreed",
                    "payload": {"event_type": "player_chat", "persona": "Azele", "session_id": "local-playtest"},
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertNotIn("Maz Scourgeheart", decision.response)
        self.assertNotEqual(
            decision.response,
            "Yeah, another run. Devona wants Maz Scourgeheart stopped, and honestly so do I.",
        )

    def test_active_scourge_quest_does_not_hijack_unrelated_player_chat(self) -> None:
        repeated = "Yeah, another run. Devona wants Maz Scourgeheart stopped, and honestly so do I."
        recent_reply_texts.append(repeated)
        messages = {
            "lets go": "Ready",
            "maz, huh?": "Maz",
            "i heard you the first time": "loop",
            "relax, azele": "loop",
        }

        for message, expected_fragment in messages.items():
            with self.subTest(message=message):
                decision = fallback_rule_decision(
                    event_from_game_log(
                        {
                            "sender": "Player",
                            "channel": "party",
                            "message": message,
                            "active_quest_id": 1456,
                            "active_quest_name": "A Scourge Beneath",
                            "payload": {
                                "event_type": "player_chat",
                                "persona": "Azele",
                                "session_id": "local-playtest",
                                "active_quest_id": 1456,
                                "active_quest_name": "A Scourge Beneath",
                            },
                        }
                    )
                )

                self.assertTrue(decision.should_speak)
                self.assertNotEqual(decision.response, repeated)
                self.assertIn(expected_fragment.lower(), decision.response.lower())

    def test_process_event_replaces_direct_player_duplicate_reply(self) -> None:
        repeated = "Yeah, another run. Devona wants Maz Scourgeheart stopped, and honestly so do I."
        recent_reply_texts.append(repeated)
        event = event_from_game_log(
            {
                "id": 1935,
                "sender": "Player",
                "channel": "party",
                "message": "another tunnel run",
                "active_quest_id": 1456,
                "active_quest_name": "A Scourge Beneath",
                "payload": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "session_id": "local-playtest",
                    "active_quest_id": 1456,
                    "active_quest_name": "A Scourge Beneath",
                },
            }
        )

        replies = process_event(event, record_id=1935, use_ollama=False)

        self.assertEqual(len(replies), 1)
        self.assertNotEqual(replies[0].message, repeated)
        self.assertTrue(any(word in replies[0].message.lower() for word in ["reset", "stuck", "loop"]))

    def test_recent_reply_lines_queries_supabase_even_with_local_buffer(self) -> None:
        original_settings = hermes_daemon.settings
        original_create_client = hermes_daemon.create_supabase_client
        recent_reply_texts.extend(["local one", "local two", "local three"])

        class FakeQuery:
            def select(self, *_args: object) -> "FakeQuery":
                return self

            def eq(self, *_args: object) -> "FakeQuery":
                return self

            def order(self, *_args: object, **_kwargs: object) -> "FakeQuery":
                return self

            def limit(self, *_args: object) -> "FakeQuery":
                return self

            def execute(self) -> object:
                class Response:
                    data = [{"message": "Yeah, another run. Devona wants Maz Scourgeheart stopped, and honestly so do I."}]

                return Response()

        class FakeClient:
            def table(self, _table_name: str) -> FakeQuery:
                return FakeQuery()

        try:
            hermes_daemon.settings = replace(
                original_settings,
                supabase_url="https://example.supabase.co",
                supabase_service_key="service-key",
            )
            hermes_daemon.create_supabase_client = lambda settings: FakeClient()

            lines = hermes_daemon.recent_reply_lines(limit=8)

            self.assertIn(
                "Yeah, another run. Devona wants Maz Scourgeheart stopped, and honestly so do I.",
                lines,
            )
            self.assertTrue(
                hermes_daemon.is_too_similar_to_recent_replies(
                    "Yeah, another run. Devona wants Maz Scourgeheart stopped, and honestly so do I."
                )
            )
        finally:
            hermes_daemon.settings = original_settings
            hermes_daemon.create_supabase_client = original_create_client

    def test_environment_alert_preserves_damage_metadata(self) -> None:
        event = event_from_environment_alert(
            {
                "alert_type": "under_attack",
                "severity": "HIGH",
                "message": "Azele is taking hits.",
                "payload": {
                    "player_hp": 0.34,
                    "player_hp_previous": 0.51,
                    "player_hp_drop": 0.17,
                    "hp_threshold_crossed": "35%",
                    "damage_severity": "critical",
                },
            }
        )

        self.assertEqual(event.player_hp_previous, 0.51)
        self.assertEqual(event.player_hp_drop, 0.17)
        self.assertEqual(event.hp_threshold_crossed, "35%")
        self.assertEqual(event.damage_severity, "critical")

    def test_small_under_attack_damage_still_speaks(self) -> None:
        replies = process_event(
            event_from_environment_alert(
                {
                    "id": 88,
                    "alert_type": "under_attack",
                    "severity": "NORMAL",
                    "message": "Azele is taking hits. Health is at 94 percent after a 3 percent drop.",
                    "payload": {
                        "player_hp": 0.94,
                        "player_hp_previous": 0.97,
                        "player_hp_drop": 0.03,
                        "damage_severity": "normal",
                    },
                }
            ),
            record_id=88,
            use_ollama=True,
        )

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].urgency, "HIGH")
        self.assertIn("94%", replies[0].message)
        self.assertEqual(replies[0].metadata["trigger_environment_alert_id"], 88)

    def test_critical_under_attack_bypasses_recent_speech_cooldown(self) -> None:
        world_state.last_spoken_at = time.time()

        replies = process_event(
            event_from_environment_alert(
                {
                    "id": 89,
                    "alert_type": "under_attack",
                    "severity": "HIGH",
                    "message": "Azele is taking a heavy hit.",
                    "payload": {
                        "player_hp": 0.34,
                        "player_hp_previous": 0.52,
                        "player_hp_drop": 0.18,
                        "hp_threshold_crossed": "35%",
                        "damage_severity": "critical",
                    },
                }
            ),
            record_id=89,
            use_ollama=True,
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("34%", replies[0].message)
        self.assertEqual(replies[0].metadata["trigger_environment_alert_id"], 89)

    def test_minor_under_attack_still_respects_recent_speech_cooldown(self) -> None:
        world_state.last_spoken_at = time.time()

        replies = process_event(
            event_from_environment_alert(
                {
                    "id": 90,
                    "alert_type": "under_attack",
                    "severity": "NORMAL",
                    "message": "Azele is taking a small hit.",
                    "payload": {
                        "player_hp": 0.96,
                        "player_hp_previous": 0.99,
                        "player_hp_drop": 0.03,
                        "damage_severity": "normal",
                    },
                }
            ),
            record_id=90,
            use_ollama=True,
        )

        self.assertEqual(replies, [])

    def test_fallback_rule_replies_to_near_death_damage(self) -> None:
        decision = fallback_rule_decision(
            event_from_environment_alert(
                {
                    "alert_type": "under_attack",
                    "severity": "HIGH",
                    "message": "Azele is almost down.",
                    "payload": {
                        "player_hp": 0.18,
                        "player_hp_previous": 0.31,
                        "player_hp_drop": 0.13,
                        "hp_threshold_crossed": "20%",
                        "damage_severity": "near_death",
                    },
                }
            )
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.urgency, "HIGH")
        self.assertIn("18%", decision.response)
        self.assertLessEqual(len(decision.response), 119)

    def test_recent_combat_reflection_uses_party_down_context_without_ollama(self) -> None:
        process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "Party member down.",
                    "payload": {
                        "persona": "Azele",
                        "event_type": "party_member_down",
                        "map_id": 147,
                        "map_name": "The Northlands",
                    },
                }
            ),
            use_ollama=False,
        )
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "cool. that was a tough situation earlier",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 148,
                    "map_name": "Ascalon City",
                    "session_id": "combat-reflection",
                },
            }
        )

        self.assertFalse(should_use_fast_fallback_before_ollama(event))
        replies = process_event(event, record_id=1742, use_ollama=False)

        self.assertEqual(len(replies), 1)
        self.assertIn("too close", replies[0].message.lower())
        self.assertIn("Ascalon City", replies[0].message)
        self.assertNotIn("what are we doing", replies[0].message.lower())

    def test_charr_hurt_followup_reflects_recent_combat(self) -> None:
        process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "Party member down.",
                    "payload": {
                        "persona": "Azele",
                        "event_type": "party_member_down",
                        "map_id": 147,
                        "map_name": "The Northlands",
                    },
                }
            ),
            use_ollama=False,
        )
        event = event_from_game_log(
            {
                "sender": "Player",
                "channel": "party",
                "message": "those charr hurt",
                "metadata": {
                    "event_type": "player_chat",
                    "persona": "Azele",
                    "map_id": 148,
                    "map_name": "Ascalon City",
                    "session_id": "combat-reflection",
                },
            }
        )

        self.assertFalse(should_use_fast_fallback_before_ollama(event))
        replies = process_event(event, record_id=1743, use_ollama=False)

        self.assertEqual(len(replies), 1)
        self.assertIn("Charr", replies[0].message)
        self.assertIn("hit hard", replies[0].message)
        self.assertIn("Ascalon City", replies[0].message)

    def test_synthetic_eval_smoke_passes(self) -> None:
        summary, failures = run_eval(total=100, failure_limit=10)

        self.assertEqual(summary.critical_failed, 0, failures[:3])
        self.assertGreaterEqual(summary.pass_rate, 0.995, failures[:3])

    def test_combat_started_noise_is_silent(self) -> None:
        replies = process_event(
            event_from_environment_alert(
                {
                    "alert_type": "combat_started",
                    "severity": "HIGH",
                    "message": "Combat started.",
                    "distance": 650,
                    "payload": {"hostile_count": 1, "close_hostile_count": 1, "closest_hostile_distance": 650},
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(replies, [])

    def test_combat_started_with_selected_target_quips_about_target(self) -> None:
        replies = process_event(
            event_from_environment_alert(
                {
                    "alert_type": "combat_started",
                    "severity": "HIGH",
                    "message": "Combat started with Charr Axe Fiend selected.",
                    "distance": 650,
                    "payload": {
                        "persona": "Azele",
                        "agent_id": 99,
                        "agent_name": "Charr Axe Fiend",
                        "hostile_count": 2,
                        "close_hostile_count": 1,
                        "closest_hostile_distance": 650,
                    },
                }
            ),
            use_ollama=True,
        )

        self.assertEqual([reply.message for reply in replies], ["On Charr Axe Fiend. Stay close."])

    def test_environment_alert_reply_records_source_alert_id(self) -> None:
        replies = process_event(
            event_from_environment_alert(
                {
                    "id": 5,
                    "alert_type": "combat_started",
                    "severity": "HIGH",
                    "message": "Combat started with selected target selected.",
                    "distance": 650,
                    "payload": {
                        "persona": "Azele",
                        "agent_id": 16,
                        "hostile_count": 2,
                        "close_hostile_count": 1,
                        "closest_hostile_distance": 650,
                    },
                }
            ),
            record_id=5,
            use_ollama=False,
        )

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].metadata["trigger_environment_alert_id"], 5)
        self.assertIsNone(replies[0].trigger_log_id)

    def test_environment_alert_handler_skips_alert_with_existing_reply(self) -> None:
        original_exists = hermes_daemon.reply_exists_for_environment_alert
        original_handle_event = hermes_daemon.handle_event
        calls: list[int] = []

        async def fail_if_called(*args: object, **kwargs: object) -> None:
            calls.append(1)

        hermes_daemon.reply_exists_for_environment_alert = lambda alert_id: alert_id == 5
        hermes_daemon.handle_event = fail_if_called
        try:
            asyncio.run(hermes_daemon.handle_environment_alert_payload({"record": {"id": 5, "payload": {}}}))
        finally:
            hermes_daemon.reply_exists_for_environment_alert = original_exists
            hermes_daemon.handle_event = original_handle_event

        self.assertEqual(calls, [])

    def test_target_change_without_target_or_visible_enemy_is_silent(self) -> None:
        replies = process_event(
            event_from_game_log(
                {
                    "sender": "System",
                    "channel": "system",
                    "message": "target_changed",
                    "payload": {"event_type": "target_changed"},
                }
            ),
            use_ollama=False,
        )

        self.assertEqual(replies, [])

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
