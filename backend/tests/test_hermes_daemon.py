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
from backend.shared.models import CompanionReplyInsert
from backend.hermes_daemon.daemon import (
    FILLER_OPENER_PATTERN,
    LOW_QUALITY_REPLY_PATTERNS,
    AMBIENT_HEARTBEAT_ACTIVITY_SECONDS,
    AMBIENT_QUIP_MIN_SECONDS,
    ambient_identity,
    ambient_heartbeat_reply,
    build_character_reply_prompt,
    event_from_environment_alert,
    event_from_game_log,
    extract_json_object,
    fallback_rule_decision,
    generate_tts_audio,
    gw_wiki_cache,
    gw_wiki_search_query,
    is_stale_polled_record,
    last_map_comment_by_session,
    map_comment_variant_by_session,
    likely_gw_wiki_question,
    memory_event_from,
    memory_buffers,
    memory_last_write_at,
    model_reply_has_bad_shape,
    persona_living_notes,
    prompt_relevant_memories,
    process_event,
    recent_conversation_context,
    recent_companion_context,
    recent_reply_texts,
    repair_model_reply,
    reply_expression,
    sanitize_memory_for_prompt,
    should_flush_memory_buffer,
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
        gw_wiki_cache.clear()
        last_map_comment_by_session.clear()
        map_comment_variant_by_session.clear()
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
        text = "Azele says Ascalon City is home, not Old Ascalon."

        kokoro = hermes_daemon._kokoro_tts_payload(text)
        chatterbox = hermes_daemon._chatterbox_tts_payload(text, expression="happy")

        self.assertEqual(kokoro["input"], "Azelle says As-kah-lon City is home, not Old As-kah-lon.")
        self.assertEqual(chatterbox["input"], "Azelle says As-kah-lon City is home, not Old As-kah-lon.")
        self.assertEqual(text, "Azele says Ascalon City is home, not Old Ascalon.")

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
                kokoro_tts_voice="af_bella",
            )
            hermes_daemon.generate_chatterbox_turbo_audio = lambda text, expression: None
            hermes_daemon.generate_kokoro_audio = lambda text: (b"kokoro", "audio/mpeg")

            self.assertEqual(
                generate_tts_audio("hello", expression="neutral"),
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
            hermes_daemon.generate_tts_audio = lambda text, expression: None

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
            hermes_daemon.generate_tts_audio = lambda text, expression: None

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
            hermes_daemon.generate_tts_audio = lambda text, expression: None
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
        self.assertIn("Consensual adult flirtation", prompt)
        self.assertIn("Do not moralize, refuse, scold, or redirect", prompt)
        self.assertIn("22-year-old", prompt)
        self.assertIn("normal party chat", prompt)
        self.assertIn("Plain is usually better than clever", prompt)
        self.assertIn("Make dialogue feel ongoing", prompt)
        self.assertIn("Do not end every reply with a question", prompt)
        self.assertIn("conversational handoff", prompt)
        self.assertIn("Do not overplay 'princess', 'brat', 'cute girl', or 'snarky companion'", prompt)
        self.assertIn("'ugh'", prompt)
        self.assertIn("'shut up'", prompt)
        self.assertIn("Do not start replies with filler noises", prompt)
        self.assertIn("do not make her sound overly mature", prompt.lower())
        self.assertIn("socially quick 22-year-old", prompt)
        self.assertIn("peace-talkers", prompt)
        self.assertIn("The player is not Azele", prompt)
        self.assertIn("address the player as 'you'", prompt)
        self.assertIn("made Azele drink Dwarven Ale", prompt)
        self.assertIn("react directly to how it feels", prompt)
        self.assertIn("nearest city?' -> 'Ascalon City", prompt)
        self.assertNotIn("nearest city?' -> 'Ashford", prompt)

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
        self.assertIn("continue that exchange", prompt)
        self.assertIn("I usually clear inventory", prompt)
        self.assertIn("answer that thread directly", prompt)
        self.assertIn("City air helps. What do you usually do first", recent_companion_context())

    def test_prompt_includes_recent_conversation_transcript(self) -> None:
        world_state.recent_chat_history.append("[Player]: what was that?")
        recent_reply_texts.append("I meant the Ranik soldiers were pretending not to stare.")
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
        self.assertIn("explain Azele's immediately previous line plainly", prompt)
        self.assertIn("Do not answer clarification questions with fresh quips", prompt)

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

    def test_high_confidence_gw1_context_skips_slow_ollama_path(self) -> None:
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

        self.assertTrue(should_use_fast_fallback_before_ollama(event))

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

    def test_gw1_resolver_understands_common_pre_searing_slang(self) -> None:
        cases = [
            ("what's the LDoA plan?", "Legendary Defender of Ascalon"),
            ("black dye just dropped", "Black Dye"),
            ("ooo a pruple thing", "Purple rarity loot"),
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
                lambda prompt, timeout_seconds=None: prompts.append(prompt)
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

        self.assertEqual(
            [reply.message for reply in replies],
            ["Yes. Charr threaten Ascalon. We prepare, then go past the Wall."],
        )
        self.assertEqual(len(prompts), 0)

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

        self.assertIn("If the player congratulates Azele for leveling up", prompt)
        self.assertIn("Do not connect level-up to red irises", prompt)
        self.assertIn("congrats, you hit level 14", prompt)

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
                lambda prompt, timeout_seconds=None: prompts.append(prompt)
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

        self.assertEqual(
            [reply.message for reply in replies],
            ["We wouldn’t. Not while they’re threatening Ascalon. You had me worried for a second."],
        )
        self.assertEqual(len(prompts), 0)

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
        self.assertRegex(
            "Ready to settle down and wait it out until you need us more than me waiting around?",
            LOW_QUALITY_REPLY_PATTERNS,
        )

    def test_azele_rejects_repeated_filler_openers(self) -> None:
        self.assertRegex("Mhmm, I am listening.", FILLER_OPENER_PATTERN)
        self.assertRegex("mm, cute.", FILLER_OPENER_PATTERN)
        self.assertNotRegex("I know. You can say it again though.", FILLER_OPENER_PATTERN)

    def test_model_reply_shape_rejects_runons_and_dangling_splits(self) -> None:
        self.assertTrue(model_reply_has_bad_shape("I"))
        self.assertTrue(model_reply_has_bad_shape("Yeah"))
        self.assertTrue(
            model_reply_has_bad_shape(
                "I know you do like that outfit though it does fit well on me today isn't it nice hearing from someone who knows exactly why they look good here"
            )
        )
        self.assertTrue(model_reply_has_bad_shape("Feels good being home after all that travel though it looks different but"))
        self.assertTrue(
            model_reply_has_bad_shape(
                "A melandru stalker sounds better for her though, maybe she'll actually use that instead of just hoarding them like Devona does with everything else anyway."
            )
        )
        self.assertFalse(model_reply_has_bad_shape("I know. Still nice to hear."))

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

    def test_ollama_generation_includes_player_map_and_ambient_events(self) -> None:
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
        self.assertTrue(should_use_ollama_for_event(map_event))
        self.assertTrue(should_use_ollama_for_event(snapshot_event))

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

    def test_player_chat_ollama_uses_short_latency_budget(self) -> None:
        original_settings = hermes_daemon.settings
        original_generate = hermes_daemon.ollama_generate_visible
        observed: list[float | None] = []

        def fake_generate(prompt: str, *, timeout_seconds: float | None = None) -> str:
            observed.append(timeout_seconds)
            return "I know. Still nice to hear."

        try:
            hermes_daemon.settings = replace(
                original_settings,
                hermes_player_chat_ollama_timeout_seconds=8.0,
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
        self.assertIn(replies[0].message, {
            "City air helps. What do you usually do first when you get back here?",
            "I keep recognizing faces here. Comforting, mostly. Do you ever get that?",
            "If we stay too long, I’m going to start fussing with my hair, and then you have to pretend not to notice.",
        })

    def test_map_entry_uses_ollama_generation_when_enabled(self) -> None:
        original = hermes_daemon.character_reply_with_ollama
        hermes_daemon.character_reply_with_ollama = lambda event: hermes_daemon.HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response="Generated map thought. Where first?",
        )
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

        self.assertEqual([reply.message for reply in replies], ["Generated map thought. Where first?"])

    def test_map_entry_drops_stale_reply_when_player_speaks_during_generation(self) -> None:
        original = hermes_daemon.character_reply_with_ollama

        def generate_after_player_chat(event: hermes_daemon.TelemetryEvent) -> hermes_daemon.HermesDecision:
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

        self.assertEqual(replies, [])

    def test_ambient_snapshot_uses_ollama_generation_when_enabled(self) -> None:
        original = hermes_daemon.character_reply_with_ollama
        hermes_daemon.character_reply_with_ollama = lambda event: hermes_daemon.HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response="Generated quiet moment. What are you watching?",
        )
        try:
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
            hermes_daemon.character_reply_with_ollama = original

        self.assertEqual([reply.message for reply in replies], ["Generated quiet moment. What are you watching?"])

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
        event = event_from_game_log(
            {
                "sender": "Loot",
                "channel": "system",
                "message": "Item dropped: Purple rarity item.",
                "metadata": {"event_type": "item_drop", "persona": "Azele"},
            }
        )

        self.assertEqual(
            validate_model_reply("Purple out here? That is worth a look. What did it roll?", event),
            "Purple out here? That is worth a look. What did it roll?",
        )

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

    def test_ambient_heartbeat_uses_ollama_generation_when_enabled(self) -> None:
        now = time.time()
        world_state.persona = "Azele"
        world_state.session_id = "ambient-heartbeat-llm"
        world_state.map_id = 148
        world_state.map_name = "Ascalon City"
        world_state.last_interaction_timestamp = now - (AMBIENT_HEARTBEAT_ACTIVITY_SECONDS / 2)
        world_state.last_spoken_at = now - (AMBIENT_QUIP_MIN_SECONDS + 1)

        original = hermes_daemon.character_reply_with_ollama
        hermes_daemon.character_reply_with_ollama = lambda event: hermes_daemon.HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response="Generated heartbeat line. Still with me?",
        )
        try:
            reply = ambient_heartbeat_reply(now=now, use_ollama=True)
        finally:
            hermes_daemon.character_reply_with_ollama = original

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertEqual(reply.message, "Generated heartbeat line. Still with me?")
        self.assertEqual(reply.metadata["trigger"], "ambient_heartbeat")

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
