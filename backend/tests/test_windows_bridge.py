from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.windows_bridge.app import app


class _FakeTable:
    def insert(self, _payload):
        return self

    def execute(self):
        return type("Response", (), {"data": []})()


class _FakeSupabase:
    def table(self, _name):
        return _FakeTable()


class _FakeReplyTable:
    def __init__(self):
        self.updated_ids = []

    def select(self, _columns):
        return self

    def is_(self, _column, _value):
        return self

    def order(self, _column, desc=False):
        return self

    def limit(self, _limit):
        return self

    def eq(self, _column, _value):
        return self

    def update(self, _payload):
        return self

    def in_(self, _column, values):
        self.updated_ids = values
        return self

    def execute(self):
        return type(
            "Response",
            (),
            {
                "data": [
                    {
                        "id": 10,
                        "persona": "A Test",
                        "message": "I hear you.",
                        "channel": "party",
                        "payload": {
                            "session_id": "local-playtest",
                            "audio_url": "https://example.supabase.co/storage/v1/object/sign/playmate-tts/test.mp3",
                            "audio_mime_type": "audio/mpeg",
                            "multi_message": True,
                            "line_index": 1,
                            "line_count": 2,
                            "reply_delay_ms": 0,
                            "post_play_delay_ms": 6200,
                        },
                    }
                ]
            },
        )()


class _FakeReplySupabase:
    def __init__(self):
        self.reply_table = _FakeReplyTable()

    def table(self, _name):
        return self.reply_table


class WindowsBridgeTests(unittest.TestCase):
    def test_health(self) -> None:
        client = TestClient(app)

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_post_event_accepts_plugin_payload(self) -> None:
        client = TestClient(app)
        payload = {
            "source": "gwtoolboxpp-playmate",
            "persona": "A Test",
            "client_time": "2026-06-26T12:00:00Z",
            "event_type": "player_chat",
            "sender": "Player",
            "channel": "party",
            "message": "hello",
        }

        with patch("backend.windows_bridge.app._client", return_value=_FakeSupabase()):
            response = client.post("/v1/playmate/events", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": True})

    def test_post_event_suppresses_noisy_quest_details(self) -> None:
        client = TestClient(app)
        payload = {
            "source": "gwtoolboxpp-playmate",
            "persona": "A Test",
            "client_time": "2026-06-26T12:00:00Z",
            "event_type": "quest_details_changed",
            "sender": "System",
            "channel": "system",
            "message": "quest_details_changed",
        }

        response = client.post("/v1/playmate/events", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": False, "reason": "suppressed_event_type"})

    def test_post_event_accepts_control_char_in_quest_payload(self) -> None:
        client = TestClient(app)
        payload = (
            '{"source":"gwtoolboxpp-playmate","persona":"A Test",'
            '"client_time":"2026-06-26T12:00:00Z","event_type":"player_chat",'
            '"sender":"Player","channel":"party","message":"hello",'
            '"active_quest_objectives":"encoded\u0001quest"}'
        )

        with patch("backend.windows_bridge.app._client", return_value=_FakeSupabase()):
            response = client.post(
                "/v1/playmate/events",
                content=payload.encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": True})

    def test_post_event_routes_environment_alert(self) -> None:
        client = TestClient(app)
        payload = {
            "source": "gwtoolboxpp-playmate",
            "persona": "A Test",
            "client_time": "2026-06-26T12:00:00Z",
            "event_type": "environment_alert",
            "sender": "System",
            "channel": "system",
            "message": "Enemy nearby.",
            "alert_type": "enemy_patrol_nearby",
            "severity": "NORMAL",
            "map_id": 148,
            "hostile_count": 2,
            "close_hostile_count": 1,
        }

        with patch("backend.windows_bridge.app._client", return_value=_FakeSupabase()):
            response = client.post("/v1/playmate/events", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": True})

    def test_get_replies_returns_audio_reply_items(self) -> None:
        client = TestClient(app)
        fake = _FakeReplySupabase()

        with patch("backend.windows_bridge.app._client", return_value=fake):
            response = client.get("/v1/playmate/replies?persona=A%20Test")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["replies"], ["I hear you."])
        self.assertEqual(body["reply_items"][0]["message"], "I hear you.")
        self.assertEqual(body["reply_items"][0]["audio_mime_type"], "audio/mpeg")
        self.assertTrue(body["reply_items"][0]["multi_message"])
        self.assertEqual(body["reply_items"][0]["line_index"], 1)
        self.assertEqual(body["reply_items"][0]["line_count"], 2)
        self.assertEqual(body["reply_items"][0]["reply_delay_ms"], 0)
        self.assertEqual(body["reply_items"][0]["post_play_delay_ms"], 6200)
        self.assertEqual(fake.reply_table.updated_ids, [10])


if __name__ == "__main__":
    unittest.main()
