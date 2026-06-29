from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from backend.shared.constants import GAMEPLAY_EVENT_TYPES
from backend.shared.models import TelemetryEvent


@dataclass
class LiveWorldState:
    recent_chat_limit: int = 10
    recent_alert_limit: int = 8
    current_zone: str = "Unknown"
    map_id: int = 0
    map_name: str = ""
    instance_type: int = 0
    active_quest_id: int = 0
    active_quest_name: str = ""
    active_quest_objectives: str = ""
    hostile_count: int = 0
    close_hostile_count: int = 0
    closest_hostile_distance: float = 0.0
    player_hp: float = 0.0
    persona: str = "Unknown Character"
    session_id: str = "local-playtest"
    recent_chat_history: deque[str] = field(default_factory=deque)
    recent_alerts: deque[dict[str, Any]] = field(default_factory=deque)
    last_interaction_timestamp: float = 0.0
    last_player_chat_at: float = 0.0
    last_spoken_at: float = 0.0

    def apply_event(self, event: TelemetryEvent) -> None:
        self.persona = event.persona or self.persona
        self.session_id = event.session_id or self.session_id
        self.map_id = event.map_id
        self.map_name = event.map_name or self.map_name
        self.instance_type = event.instance_type
        self.active_quest_id = event.active_quest_id
        self.active_quest_name = event.active_quest_name
        self.active_quest_objectives = event.active_quest_objectives
        self.hostile_count = event.hostile_count
        self.close_hostile_count = event.close_hostile_count
        self.closest_hostile_distance = event.closest_hostile_distance
        self.player_hp = event.player_hp
        observed_at = time.time()
        self.last_interaction_timestamp = observed_at

        if event.event_type in {"player_chat", "chat_log"}:
            self.recent_chat_history.append(f"[{event.sender}]: {event.message}")
            while len(self.recent_chat_history) > self.recent_chat_limit:
                self.recent_chat_history.popleft()
            if event.event_type == "player_chat" and event.channel == "party":
                self.last_player_chat_at = observed_at

        if event.event_type == "environment_alert" or event.event_type in GAMEPLAY_EVENT_TYPES:
            self.recent_alerts.append(event.metadata())
            while len(self.recent_alerts) > self.recent_alert_limit:
                self.recent_alerts.popleft()

    def can_speak(self, minimum_seconds: float) -> bool:
        return time.time() - self.last_spoken_at >= minimum_seconds

    def mark_spoken(self) -> None:
        self.last_spoken_at = time.time()

    def compact_after_memory_flush(self) -> None:
        while len(self.recent_chat_history) > max(3, self.recent_chat_limit // 2):
            self.recent_chat_history.popleft()
        while len(self.recent_alerts) > max(3, self.recent_alert_limit // 2):
            self.recent_alerts.popleft()

    def prompt_context(self) -> str:
        chat_lines = list(self.recent_chat_history)[-6:]
        chat = "\n".join(chat_lines) or "None"
        alert_lines: list[str] = []
        for alert in list(self.recent_alerts)[-5:]:
            event_type = alert.get("alert_type") or alert.get("event_type") or "event"
            message = alert.get("message") or alert.get("agent_name") or ""
            hostiles = alert.get("close_hostile_count") or alert.get("hostile_count") or 0
            if message and hostiles:
                alert_lines.append(f"{event_type}: {message} ({hostiles} hostiles)")
            elif message:
                alert_lines.append(f"{event_type}: {message}")
            else:
                alert_lines.append(str(event_type))
        alerts = "\n".join(alert_lines) or "None"
        return (
            f"Persona: {self.persona}\n"
            f"Map: {self.map_name or 'Unknown'} ({self.map_id})\n"
            f"Instance Type: {self.instance_type}\n"
            f"Active Quest: {self.active_quest_id} {self.active_quest_name}\n"
            f"Quest Objectives: {self.active_quest_objectives or 'None'}\n"
            f"Hostiles: {self.hostile_count} total, {self.close_hostile_count} close, closest {self.closest_hostile_distance:.0f}\n"
            f"Player HP: {self.player_hp:.0%}\n"
            f"Recent Chat:\n{chat}\n"
            f"Recent Alerts:\n{alerts}\n"
        )
