from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    source: str = "gwtoolboxpp-playmate"
    persona: str = "Unknown Character"
    client_time: str | None = None
    event_type: str
    sender: str
    channel: str
    message: str
    map_id: int = 0
    map_name: str = ""
    instance_type: int = 0
    district: int = 0
    instance_time: int = 0
    active_quest_id: int = 0
    quest_count: int = 0
    active_quest_name: str = ""
    active_quest_objectives: str = ""
    player_x: float = 0.0
    player_y: float = 0.0
    player_hp: float = 0.0
    hostile_count: int = 0
    close_hostile_count: int = 0
    dead_hostile_count: int = 0
    closest_hostile_agent_id: int = 0
    closest_hostile_distance: float = 0.0
    alert_type: str = ""
    severity: str = "NORMAL"
    agent_id: int = 0
    agent_name: str = ""
    objective_id: int = 0
    objective_name: str = ""
    progress_current: float = 0.0
    progress_total: float = 0.0
    foes_killed: int = 0
    foes_remaining: int = 0
    session_id: str = "local-playtest"

    @field_validator("event_type", "sender", "channel", "message")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("channel", mode="before")
    @classmethod
    def normalize_channel(cls, value: Any) -> str:
        return str(value).strip().lower()

    @field_validator("event_type", mode="before")
    @classmethod
    def normalize_event_type(cls, value: Any) -> str:
        return str(value).strip().lower()

    def metadata(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "persona": self.persona,
            "client_time": self.client_time,
            "event_type": self.event_type,
            "map_id": self.map_id,
            "map_name": self.map_name,
            "instance_type": self.instance_type,
            "district": self.district,
            "instance_time": self.instance_time,
            "active_quest_id": self.active_quest_id,
            "quest_count": self.quest_count,
            "active_quest_name": self.active_quest_name,
            "active_quest_objectives": self.active_quest_objectives,
            "player_x": self.player_x,
            "player_y": self.player_y,
            "player_hp": self.player_hp,
            "hostile_count": self.hostile_count,
            "close_hostile_count": self.close_hostile_count,
            "dead_hostile_count": self.dead_hostile_count,
            "closest_hostile_agent_id": self.closest_hostile_agent_id,
            "closest_hostile_distance": self.closest_hostile_distance,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "objective_id": self.objective_id,
            "objective_name": self.objective_name,
            "progress_current": self.progress_current,
            "progress_total": self.progress_total,
            "foes_killed": self.foes_killed,
            "foes_remaining": self.foes_remaining,
            "session_id": self.session_id,
        }

    def to_game_log_insert(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "event_type": self.event_type,
            "sender": self.sender,
            "message": self.message,
            "channel": self.channel,
            "map_id": self.map_id,
            "instance_type": self.instance_type,
            "district": self.district,
            "instance_time": self.instance_time,
            "active_quest_id": self.active_quest_id,
            "quest_count": self.quest_count,
            "active_quest_name": self.active_quest_name,
            "active_quest_objectives": self.active_quest_objectives,
            "payload": self.metadata(),
        }

    def to_environment_alert_insert(self) -> dict[str, Any]:
        metadata = self.metadata()
        return {
            "alert_type": self.alert_type or self.event_type,
            "severity": self.severity or "NORMAL",
            "map_id": self.map_id or None,
            "player_x": self.player_x or None,
            "player_y": self.player_y or None,
            "agent_id": self.closest_hostile_agent_id or None,
            "distance": self.closest_hostile_distance or None,
            "faction": "enemy" if self.hostile_count else None,
            "message": self.message,
            "payload": metadata,
        }


class CompanionReplyInsert(BaseModel):
    persona: str
    message: str
    channel: str = "party"
    session_id: str = "local-playtest"
    urgency: str = "NORMAL"
    trigger_log_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_supabase_insert(self) -> dict[str, Any]:
        row = {
            "persona": self.persona,
            "message": self.message,
            "channel": self.channel,
            "payload": {
                "session_id": self.session_id,
                "urgency": self.urgency,
                **self.metadata,
            },
        }
        if self.trigger_log_id is not None:
            row["trigger_log_id"] = self.trigger_log_id
            row["payload"]["trigger_log_id"] = self.trigger_log_id
        return row


class CompanionReplyRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    persona: str | None = None
    message: str
    channel: str = "party"
    session_id: str | None = None
    urgency: str | None = None
    consumed_at: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def payload_session_id(self) -> str | None:
        return self.session_id or self.payload.get("session_id")


class RepliesResponse(BaseModel):
    replies: list[str] = Field(default_factory=list)


class MemoryInsert(BaseModel):
    character_name: str
    summary_text: str
    session_id: str = "local-playtest"
    memory_type: str = "session_summary"
    title: str | None = None
    map_id: int | None = None
    active_quest_id: int | None = None
    rare_items: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_log_start_id: int | None = None
    source_log_end_id: int | None = None
    embedding: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("character_name", "summary_text", "memory_type")
    @classmethod
    def require_memory_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value.strip()

    def to_supabase_insert(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "character_name": self.character_name,
            "session_id": self.session_id,
            "memory_type": self.memory_type,
            "title": self.title,
            "summary_text": self.summary_text,
            "map_id": self.map_id,
            "active_quest_id": self.active_quest_id,
            "rare_items": self.rare_items,
            "tags": self.tags,
            "source_log_start_id": self.source_log_start_id,
            "source_log_end_id": self.source_log_end_id,
            "metadata": self.metadata,
        }
        if self.embedding is not None:
            row["embedding"] = self.embedding
        return row


class HermesDecision(BaseModel):
    should_speak: bool = False
    channel_override: Literal["CHANNEL_PARTY", "CHANNEL_LOCAL", "CHANNEL_SYSTEM"] = "CHANNEL_LOCAL"
    urgency: Literal["LOW", "NORMAL", "HIGH"] = "NORMAL"
    response: str = ""

    @field_validator("response")
    @classmethod
    def trim_response(cls, value: str) -> str:
        return value.strip()

    def to_reply(
        self,
        persona: str,
        session_id: str,
        trigger_log_id: int | None = None,
    ) -> CompanionReplyInsert | None:
        if not self.should_speak or not self.response:
            return None
        channel_map = {
            "CHANNEL_PARTY": "party",
            "CHANNEL_LOCAL": "local",
            "CHANNEL_SYSTEM": "system",
        }
        return CompanionReplyInsert(
            persona=persona,
            message=self.response,
            channel=channel_map[self.channel_override],
            session_id=session_id,
            urgency=self.urgency,
            trigger_log_id=trigger_log_id,
            metadata={"channel_override": self.channel_override},
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
