from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

import backend.hermes.daemon as hermes_daemon
from backend.hermes_daemon.daemon import (
    FILLER_OPENER_PATTERN,
    LOW_QUALITY_REPLY_PATTERNS,
    build_character_reply_prompt,
    event_from_environment_alert,
    event_from_game_log,
    extract_json_object,
    fallback_rule_decision,
    gw_wiki_cache,
    gw_wiki_search_query,
    is_stale_polled_record,
    last_map_comment_by_session,
    likely_gw_wiki_question,
    model_reply_has_bad_shape,
    process_event,
    recent_reply_texts,
    should_use_ollama_for_event,
    world_state,
)


class HermesDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        recent_reply_texts.clear()
        gw_wiki_cache.clear()
        last_map_comment_by_session.clear()
        world_state.last_spoken_at = 0

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

    def test_azele_rejects_sanitized_refusal_replies(self) -> None:
        self.assertRegex("I can't engage with that, keep things appropriate.", LOW_QUALITY_REPLY_PATTERNS)

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

        self.assertEqual(
            [reply.message for reply in replies],
            ["Yes. Charr threaten Ascalon. We prepare, then go past the Wall."],
        )

    def test_azele_rejects_saving_charr_premise(self) -> None:
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

        self.assertEqual(
            [reply.message for reply in replies],
            ["We wouldn’t. Not while they’re threatening Ascalon. You had me worried for a second."],
        )

    def test_azele_rejects_overly_mature_old_voice(self) -> None:
        self.assertRegex("Very undignified of me, tragically.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Obviously. I have a whole vibe to protect.", LOW_QUALITY_REPLY_PATTERNS)
        self.assertRegex("Praise accepted. Keep it cute.", LOW_QUALITY_REPLY_PATTERNS)

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
