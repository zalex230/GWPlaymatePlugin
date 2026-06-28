from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import os
import time
import unittest

os.environ["GWPLAYMATE_DISABLE_MEMORY_WRITES"] = "1"

import backend.hermes.daemon as hermes_daemon
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
    should_flush_memory_buffer,
    should_use_ollama_for_event,
    world_state,
)


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
        self.assertIn("the player is the player, not Azele", prompt)
        self.assertIn("address the player as 'you'", prompt)

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

    def test_azele_rejects_sanitized_refusal_replies(self) -> None:
        self.assertRegex("I can't engage with that, keep things appropriate.", LOW_QUALITY_REPLY_PATTERNS)

    def test_azele_rejects_scolding_question_fallback_shape(self) -> None:
        self.assertRegex("Maybe. Ask me properly and I’ll answer properly.", LOW_QUALITY_REPLY_PATTERNS)

    def test_azele_rejects_alex_identity_confusion(self) -> None:
        self.assertRegex("You want me to help carry his gear or just let the player handle it?", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("I am the player, so I can carry it.", LOW_QUALITY_REPLY_PATTERNS)

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
            hermes_daemon.ollama_generate_visible = lambda prompt: prompts.append(prompt) or "Yes. They threaten Ascalon, so we prepare and hit them properly."
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
            ["Yes. They threaten Ascalon, so we prepare and hit them properly."],
        )
        self.assertEqual(len(prompts), 1)
        self.assertIn("defending Ascalon", prompts[0])

    def test_azele_rejects_saving_charr_premise(self) -> None:
        prompts: list[str] = []
        original_generate = hermes_daemon.ollama_generate_visible
        try:
            hermes_daemon.ollama_generate_visible = lambda prompt: prompts.append(prompt) or "We would not. Not while they are threatening Ascalon."
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
            ["We would not. Not while they are threatening Ascalon."],
        )
        self.assertEqual(len(prompts), 1)
        self.assertIn("Never imply Charr need saving", prompts[0])

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
            "Good. Clear the bags first, then we move cleaner.",
            "That makes sense. Less rummaging while something is trying to kill us.",
            "Practical. I like it when preparation saves us embarrassment later.",
        })

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

    def test_azele_rejects_overly_mature_old_voice(self) -> None:
        self.assertRegex("Very undignified of me, tragically.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Obviously. I have a whole vibe to protect.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Praise accepted. Keep it cute.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("The Northlands is no place for peace-talkers.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Lead me on; let's get those irises.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Let's get those irises before they move away from us.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Keep your shield ready when we hit that line again.", LOW_QUALITY_REPLY_PATTERNS)

    def test_azele_rejects_repeated_filler_openers(self) -> None:
        self.assertRegex("Mhmm, I am listening.", FILLER_OPENER_PATTERN)
        self.assertRegex("mm, cute.", FILLER_OPENER_PATTERN)
        self.assertNotRegex("I know. You can say it again though.", FILLER_OPENER_PATTERN)

    def test_model_reply_shape_rejects_runons_and_dangling_splits(self) -> None:
        self.assertTrue(
            model_reply_has_bad_shape(
                "I know you do like that outfit though it does fit well on me today isn't it nice hearing from someone who knows exactly why they look good here"
            )
        )
        self.assertTrue(model_reply_has_bad_shape("Feels good being home after all that travel though it looks different but"))
        self.assertFalse(model_reply_has_bad_shape("I know. Still nice to hear."))

    def test_ollama_generation_is_only_for_player_chat(self) -> None:
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

        self.assertTrue(should_use_ollama_for_event(chat_event))
        self.assertFalse(should_use_ollama_for_event(map_event))

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

    def test_azele_persona_loads_personal_memory_doc(self) -> None:
        notes = persona_living_notes("Azele")

        self.assertIn("Personal memory notes", notes)
        self.assertIn("About the player", notes)
        self.assertIn("listen to the current exchange first", notes)

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
                summary_text='the player told Azele: "remember that I like checking every hidden stash".',
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
