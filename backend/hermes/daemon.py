from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from supabase import acreate_client
import uvicorn
from fastapi import FastAPI

from backend.shared.config import load_settings
from backend.shared.constants import COMPANION_REPLIES_TABLE, ENVIRONMENT_ALERTS_TABLE, GAME_LOGS_TABLE, MEMORIES_TABLE
from backend.shared.models import (
    CompanionReplyInsert,
    HermesDecision,
    HermesEventResponse,
    MemoryInsert,
    MemoryRow,
    TelemetryEvent,
    utc_now_iso,
)
from backend.shared.state import LiveWorldState
from backend.shared.supabase_client import create_supabase_client, require_supabase_settings
from backend.hermes.gw1_knowledge import ResolvedGameContext, resolve_gw1_context


settings = load_settings()
DAEMON_STARTED_AT = datetime.now(timezone.utc)
app = FastAPI(title="GWPlaymate Hermes", version="0.1.0")
world_state_lock = RLock()
ollama_request_lock = Lock()
world_state = LiveWorldState(
    recent_chat_limit=settings.recent_chat_limit,
    recent_alert_limit=settings.recent_alert_limit,
    session_id=settings.active_session,
)
recent_reply_texts: deque[str] = deque(maxlen=12)
map_comment_variant_by_session: dict[tuple[str, str, int], int] = {}
ambient_quip_variant_by_session: dict[tuple[str, str, int], int] = {}
MAX_GW_CHAT_CHARS = 119
MAX_GW_REPLY_LINES = 8
MULTI_MESSAGE_MIN_REPLY_DELAY_MS = 3200
MULTI_MESSAGE_MAX_REPLY_DELAY_MS = 14000
VISIBLE_ENEMY_RANGE = 900.0
AMBIENT_QUIP_MIN_SECONDS = 55.0
AMBIENT_AFTER_PLAYER_CHAT_QUIET_SECONDS = 55.0
AMBIENT_HEARTBEAT_POLL_SECONDS = 10.0
AMBIENT_HEARTBEAT_ACTIVITY_SECONDS = 600.0
UNCONSUMED_REPLY_STALE_SECONDS = 60.0
MAP_ENTRY_AFTER_PLAYER_CHAT_QUIET_SECONDS = 35.0
PERSONA_MEMORY_DIR = Path(__file__).with_name("personas")
GW_WIKI_API_URL = "https://wiki.guildwars.com/api.php"
GW_WIKI_PAGE_URL = "https://wiki.guildwars.com/wiki/{title}"
GW_WIKI_TIMEOUT_SECONDS = 4.0
GW_WIKI_CACHE_SECONDS = 3600.0
DEFAULT_POLL_STATE_PATH = Path(__file__).resolve().parents[1] / ".state" / "hermes_poll_watermarks.json"
PROACTIVE_EVENT_TYPES = {
    "active_quest_changed",
    "environment_alert",
    "item_drop",
    "mission_objective_added",
    "mission_objective_completed",
    "mission_objective_updated",
    "target_changed",
    "party_member_down",
    "party_member_recovered",
    "vanquish_complete",
    "vanquish_progress",
    "map_changed",
    "map_change",
    "map_loaded",
    "snapshot",
}
SPEAKING_ENVIRONMENT_ALERT_TYPES = {
    "under_attack",
    "danger_spike",
    "party_member_down",
    "combat_started",
    "combat_over",
    "status_effect",
}
EMERGENCY_ALERT_TYPES = {"under_attack", "party_member_down", "combat_started", "status_effect"}
NOTABLE_CHAT_PATTERNS = re.compile(
    r"\b("
    r"gold|green|unique|rare|drop|dropped|item|chest|boss|elite|skill|quest|completed|morale|death|died|resurrect|shrine|"
    r"upgrade|armor|armour|leggings?|krytan"
    r")\b",
    re.IGNORECASE,
)
UNSUPPORTED_AMBIENT_LOOT_PATTERN = re.compile(
    r"\b("
    r"purple|"
    r"gold\s+rarity|"
    r"green\s+rarity|"
    r"unique\s+loot|"
    r"loot|"
    r"drops?|"
    r"dropped|"
    r"item|"
    r"chests?|"
    r"stash(?:es)?|"
    r"roll(?:ed|s)?|"
    r"go\s+check\s+it|"
    r"worth\s+stopping\s+for"
    r")\b",
    re.IGNORECASE,
)
UNSUPPORTED_SELF_DUPLICATE_PATTERN = re.compile(
    r"\b(?:someone|somebody|anyone)\s+else\s+(?:who\s+)?(?:looks?|looking)\s+like\s+me\b",
    re.IGNORECASE,
)
NPC_DIALOGUE_CHANNELS = {"local", "emote"}
NPC_DIALOGUE_IGNORE_PATTERNS = re.compile(
    r"\b(?:gwtoolbox|plugins detected|trade|wts|wtb|lfg|district|server|error)\b",
    re.IGNORECASE,
)
LOW_QUALITY_REPLY_PATTERNS = re.compile(
    r"\b("
    r"don'?t get ahead of yourself|"
    r"not some prize|"
    r"easy openings|"
    r"counts as a victory|"
    r"as an ai|"
    r"i can'?t (?:engage|help|assist)|"
    r"i won'?t (?:engage|help|assist)|"
    r"not appropriate|"
    r"keep it appropriate|"
    r"keep things appropriate|"
    r"change the subject|"
    r"when did this happen|"
    r"seen a war|"
    r"sound like you'?ve seen|"
    r"very undignified|"
    r"tragically|"
    r"tasteful admiration|"
    r"image to maintain|"
    r"my brilliance|"
    r"looking this composed|"
    r"whole vibe|"
    r"keep it cute|"
    r"getting bored being cute|"
    r"compliments make me worse|"
    r"adorable and difficult|"
    r"ask me properly|"
    r"what are you actually asking|"
    r"peace-talkers?|"
    r"lead me on|"
    r"before they move away from us|"
    r"hit that line again|"
    r"no place for peace-talkers|"
    r"alex(?:ie|i)?|"
    r"\bthe\s+player\b|"
    r"ready\s+to\s+settle\s+down|"
    r"wait\s+it\s+out\s+until\s+you\s+need|"
    r"need\s+us\s+more\s+than\s+me\s+waiting\s+around|"
    r"for now$|"
    r"i told you what mine meant"
    r")\b",
    re.IGNORECASE,
)
FILLER_ONLY_REPLY_PATTERN = re.compile(r"^\s*(?:m+h+m+|m+h+mm+|m+hm+|mm+|hm+)[,.\s]*(?:yeah|okay|cute)?[.!?]?\s*$", re.IGNORECASE)
DANGLING_REPLY_ENDING_PATTERN = re.compile(
    r"(?:\b(?:and|but|or|so|because|before|after|when|while|although|if|until|like|than|just|the|a|an|to|of|for|with|from|by|what|who|where|why|how)|\b(?:not\s+worth|so|because|then|and|but)\s*(?:we|i|you|they|he|she|it)?)$",
    re.IGNORECASE,
)
DANGLING_SPLIT_ENDING_PATTERN = re.compile(
    r"\b(?:and|but|or|so|because|before|after|when|while|though|although|if|until|like|than|just|the|a|an|to|of|for|with|from|by|on|in|at|into|over|under|near|toward|towards)$",
    re.IGNORECASE,
)
MEMORY_MEANINGFUL_EVENT_TYPES = {
    "player_chat",
    "chat_log",
    "npc_speech_bubble",
    "active_quest_changed",
    "environment_alert",
    "item_drop",
    "target_changed",
    "party_member_down",
    "party_defeated",
    "mission_objective_completed",
    "mission_objective_updated",
    "vanquish_complete",
    "vanquish_progress",
}
MEMORY_MAP_EVENT_TYPES = {"map_changed", "map_change", "map_loaded"}
MEMORY_MIN_EVENTS = 6
MEMORY_MAX_EVENTS = 12
MEMORY_MIN_SECONDS = 35.0
memory_buffers: dict[tuple[str, str], deque[dict[str, Any]]] = {}
memory_last_write_at: dict[tuple[str, str], float] = {}
memory_lock = Lock()
last_map_comment_by_session: dict[tuple[str, str], int] = {}
gw_wiki_cache: dict[str, tuple[float, str]] = {}
MAP_COMMENT_EVENT_TYPES = {"map_loaded"}
KNOWN_PRESEARING_MAP_NAMES = {
    146: "Lakeside County",
    147: "The Northlands",
    148: "Ascalon City",
    151: "The Catacombs",
    161: "Wizard's Folly",
    165: "Foible's Fair",
    166: "Green Hills County",
    168: "Regent Valley",
    170: "Ashford Abbey",
    171: "Foible's Fair",
    172: "Fort Ranik",
}
DURABLE_PLAYER_MEMORY_PATTERNS = re.compile(
    r"\b("
    r"remember|don't forget|do not forget|important|for later|from now on|"
    r"i prefer|i like|i love|i hate|my favorite|call me|"
    r"we decided|we learned|we found|we discovered|we met|"
    r"i want you to|you should know|that matters|this matters"
    r")\b",
    re.IGNORECASE,
)
NOTABLE_DISCOVERY_PATTERNS = re.compile(
    r"\b("
    r"rare|purple|gold|green|unique|boss|elite|tome|dye|black dye|"
    r"completed|quest complete|reward|upgrade|armor|armour|leggings?|krytan|discovered|found|learned|met|"
    r"rurik|devona|mhenlo|cynn|charr|northlands|catacombs"
    r")\b",
    re.IGNORECASE,
)


def readable_game_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    printable = sum(1 for char in text if char.isascii() and (char.isprintable() or char.isspace()))
    if printable / max(len(text), 1) < 0.85:
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) < 2:
        return ""
    return cleaned[:160]


def event_debug_label(event: TelemetryEvent, *, record_id: int | None = None) -> str:
    parts = [
        f"record_id={record_id}" if record_id is not None else "record_id=none",
        f"event_type={event.event_type or 'unknown'}",
        f"channel={event.channel or 'unknown'}",
    ]
    if event.map_id is not None:
        parts.append(f"map_id={event.map_id}")
    message = readable_game_text(event.message)
    if message:
        parts.append(f"message={message!r}")
    return " ".join(parts)


def event_from_game_log(record: dict[str, Any]) -> TelemetryEvent:
    metadata = record.get("payload") or record.get("metadata") or {}
    return TelemetryEvent(
        source=record.get("source") or metadata.get("source", "supabase-game-log"),
        persona=metadata.get("persona", record.get("sender") or "Unknown Character"),
        client_time=metadata.get("client_time"),
        event_type=record.get("event_type") or metadata.get("event_type", "game_log"),
        sender=record.get("sender") or "Game",
        channel=record.get("channel") or "system",
        message=record.get("message") or "",
        map_id=record.get("map_id") or metadata.get("map_id", 0),
        map_name=readable_game_text(record.get("map_name") or metadata.get("map_name", "")),
        instance_type=record.get("instance_type") or metadata.get("instance_type", 0),
        district=record.get("district") or metadata.get("district", 0),
        instance_time=record.get("instance_time") or metadata.get("instance_time", 0),
        active_quest_id=record.get("active_quest_id") or metadata.get("active_quest_id", 0),
        quest_count=record.get("quest_count") or metadata.get("quest_count", 0),
        active_quest_name=readable_game_text(record.get("active_quest_name") or metadata.get("active_quest_name", "")),
        active_quest_objectives=readable_game_text(
            record.get("active_quest_objectives") or metadata.get("active_quest_objectives", "")
        ),
        player_x=metadata.get("player_x", record.get("player_x") or 0),
        player_y=metadata.get("player_y", record.get("player_y") or 0),
        player_hp=metadata.get("player_hp", 0),
        player_hp_previous=metadata.get("player_hp_previous", 0),
        player_hp_drop=metadata.get("player_hp_drop", 0),
        hp_threshold_crossed=metadata.get("hp_threshold_crossed", ""),
        damage_severity=metadata.get("damage_severity", ""),
        effect_type=metadata.get("effect_type", record.get("effect_type") or ""),
        effect_name=metadata.get("effect_name", record.get("effect_name") or ""),
        effect_source=metadata.get("effect_source", record.get("effect_source") or ""),
        hostile_count=metadata.get("hostile_count", 0),
        close_hostile_count=metadata.get("close_hostile_count", 0),
        dead_hostile_count=metadata.get("dead_hostile_count", 0),
        closest_hostile_agent_id=metadata.get("closest_hostile_agent_id", record.get("agent_id") or 0),
        closest_hostile_distance=metadata.get("closest_hostile_distance", record.get("distance") or 0),
        alert_type=metadata.get("alert_type", record.get("alert_type") or ""),
        severity=metadata.get("severity", record.get("severity") or "NORMAL"),
        agent_id=metadata.get("agent_id", record.get("agent_id") or 0),
        agent_name=readable_game_text(metadata.get("agent_name", record.get("agent_name") or "")),
        objective_id=metadata.get("objective_id", record.get("objective_id") or 0),
        objective_name=readable_game_text(metadata.get("objective_name", record.get("objective_name") or "")),
        progress_current=metadata.get("progress_current", record.get("progress_current") or 0),
        progress_total=metadata.get("progress_total", record.get("progress_total") or 0),
        foes_killed=metadata.get("foes_killed", record.get("foes_killed") or 0),
        foes_remaining=metadata.get("foes_remaining", record.get("foes_remaining") or 0),
        session_id=metadata.get("session_id", settings.active_session),
    )


def event_from_environment_alert(record: dict[str, Any]) -> TelemetryEvent:
    metadata = record.get("payload") or {}
    return TelemetryEvent(
        source=metadata.get("source", "supabase-environment-alert"),
        persona=metadata.get("persona", "Unknown Character"),
        client_time=metadata.get("client_time"),
        event_type="environment_alert",
        sender="System",
        channel="system",
        message=record.get("message") or metadata.get("message") or record.get("alert_type") or "environment_alert",
        map_id=record.get("map_id") or metadata.get("map_id", 0),
        map_name=readable_game_text(record.get("map_name") or metadata.get("map_name", "")),
        instance_type=metadata.get("instance_type", 0),
        district=metadata.get("district", 0),
        instance_time=metadata.get("instance_time", 0),
        active_quest_id=metadata.get("active_quest_id", 0),
        quest_count=metadata.get("quest_count", 0),
        active_quest_name=readable_game_text(metadata.get("active_quest_name", "")),
        active_quest_objectives=readable_game_text(metadata.get("active_quest_objectives", "")),
        player_x=metadata.get("player_x", record.get("player_x") or 0),
        player_y=metadata.get("player_y", record.get("player_y") or 0),
        player_hp=metadata.get("player_hp", 0),
        player_hp_previous=metadata.get("player_hp_previous", 0),
        player_hp_drop=metadata.get("player_hp_drop", 0),
        hp_threshold_crossed=metadata.get("hp_threshold_crossed", ""),
        damage_severity=metadata.get("damage_severity", ""),
        effect_type=metadata.get("effect_type", record.get("effect_type") or ""),
        effect_name=metadata.get("effect_name", record.get("effect_name") or ""),
        effect_source=metadata.get("effect_source", record.get("effect_source") or ""),
        hostile_count=metadata.get("hostile_count", 0),
        close_hostile_count=metadata.get("close_hostile_count", 0),
        dead_hostile_count=metadata.get("dead_hostile_count", 0),
        closest_hostile_agent_id=metadata.get("closest_hostile_agent_id", record.get("agent_id") or 0),
        closest_hostile_distance=metadata.get("closest_hostile_distance", record.get("distance") or 0),
        alert_type=metadata.get("alert_type", record.get("alert_type") or ""),
        severity=metadata.get("severity", record.get("severity") or "NORMAL"),
        agent_id=metadata.get("agent_id", record.get("agent_id") or 0),
        agent_name=readable_game_text(metadata.get("agent_name", record.get("agent_name") or "")),
        objective_id=metadata.get("objective_id", record.get("objective_id") or 0),
        objective_name=readable_game_text(metadata.get("objective_name", record.get("objective_name") or "")),
        progress_current=metadata.get("progress_current", record.get("progress_current") or 0),
        progress_total=metadata.get("progress_total", record.get("progress_total") or 0),
        foes_killed=metadata.get("foes_killed", record.get("foes_killed") or 0),
        foes_remaining=metadata.get("foes_remaining", record.get("foes_remaining") or 0),
        session_id=metadata.get("session_id", settings.active_session),
    )


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def memory_key(character_name: str, session_id: str) -> tuple[str, str]:
    character = character_name.strip() or "Unknown Character"
    session = session_id.strip() or settings.active_session
    return character, session


def memory_event_from(event: TelemetryEvent, record_id: int | None) -> dict[str, Any] | None:
    if event.event_type not in MEMORY_MEANINGFUL_EVENT_TYPES:
        return None
    message = readable_game_text(event.message) or event.event_type
    notability = ""

    if event.event_type == "player_chat":
        if DURABLE_PLAYER_MEMORY_PATTERNS.search(message):
            notability = "durable_player_note"
        elif NOTABLE_DISCOVERY_PATTERNS.search(message):
            notability = "player_noted_discovery"
        else:
            return None

    if event.event_type == "chat_log" and not (
        NOTABLE_CHAT_PATTERNS.search(message) or is_npc_dialogue_event(event)
    ):
        return None
    if event.event_type in {"chat_log", "npc_speech_bubble"}:
        if is_npc_dialogue_event(event):
            notability = "npc_dialogue"
        elif NOTABLE_DISCOVERY_PATTERNS.search(message):
            notability = "notable_game_text"
        else:
            return None
    if event.event_type == "active_quest_changed" and not readable_game_text(event.active_quest_name):
        return None
    if event.event_type == "environment_alert" and not (
        str(event.severity or "").upper() == "HIGH"
        or event.alert_type in EMERGENCY_ALERT_TYPES
        or "rare" in message.lower()
    ):
        return None
    if event.event_type == "target_changed" and not (
        readable_game_text(event.agent_name) and NOTABLE_DISCOVERY_PATTERNS.search(event.agent_name)
    ):
        return None
    if not notability:
        if event.event_type == "active_quest_changed":
            notability = "quest_context"
        elif event.event_type in {"party_member_down", "party_defeated"}:
            notability = "party_danger"
        elif event.event_type.startswith("mission_") or event.event_type.startswith("vanquish_"):
            notability = "mission_progress"
        elif event.event_type == "environment_alert":
            notability = "combat_pressure"
        elif event.event_type == "target_changed":
            notability = "notable_target"
        else:
            notability = "notable_event"
    return {
        "record_id": record_id,
        "event_type": event.event_type,
        "notability": notability,
        "message": message,
        "map_id": event.map_id or None,
        "map_name": readable_game_text(getattr(event, "map_name", "")),
        "active_quest_id": event.active_quest_id or None,
        "active_quest_name": readable_game_text(event.active_quest_name),
        "alert_type": event.alert_type,
        "severity": event.severity or "NORMAL",
        "hostile_count": event.hostile_count,
        "close_hostile_count": event.close_hostile_count,
        "channel": event.channel,
        "sender": event.sender,
        "client_time": event.client_time,
    }


def extract_rare_items(events: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    for event in events:
        message = str(event.get("message") or "").lower()
        for label in ("green", "unique", "purple", "rare", "gold"):
            if re.search(rf"\b{label}\b", message) and label not in items:
                items.append(label)
    return items[:8]


def memory_tags_for(events: list[dict[str, Any]], rare_items: list[str]) -> list[str]:
    tags: set[str] = set()
    for event in events:
        event_type = event.get("event_type")
        notability = event.get("notability")
        if event_type in MEMORY_MAP_EVENT_TYPES:
            tags.add("map_change")
        if event_type == "active_quest_changed" or event.get("active_quest_id"):
            tags.add("quest_progress")
        if event_type == "environment_alert" or event.get("hostile_count") or event.get("close_hostile_count"):
            tags.add("combat")
        if event_type == "player_chat":
            tags.add("player_chat")
        if notability == "durable_player_note":
            tags.add("relationship")
            tags.add("player_preference")
        if notability == "player_noted_discovery":
            tags.add("discovery")
        if notability == "npc_dialogue":
            tags.add("npc_dialogue")
            tags.add("world_lore")
        if notability == "mission_progress":
            tags.add("mission")
        if event_type == "target_changed":
            tags.add("target")
    if rare_items:
        tags.add("rare_item")
        tags.add("loot")
    return sorted(tags)


def memory_type_for(tags: list[str], reason: str) -> str:
    tag_set = set(tags)
    if "player_preference" in tag_set or "relationship" in tag_set:
        return "relationship_note"
    if "npc_dialogue" in tag_set:
        return "npc_dialogue"
    if "rare_item" in tag_set and len(tag_set) <= 3:
        return "rare_item"
    if reason == "high_urgency_alert" or "combat" in tag_set:
        return "combat_note"
    if "quest_progress" in tag_set and "map_change" not in tag_set:
        return "quest_progress"
    return "session_summary"


def summarize_memory_events(
    character_name: str,
    session_id: str,
    events: list[dict[str, Any]],
    *,
    reason: str,
) -> MemoryInsert | None:
    if not events:
        return None

    rare_items = extract_rare_items(events)
    tags = memory_tags_for(events, rare_items)
    memory_type = memory_type_for(tags, reason)
    latest = events[-1]
    map_id = latest.get("map_id")
    active_quest_id = latest.get("active_quest_id")
    source_ids = [event.get("record_id") for event in events if isinstance(event.get("record_id"), int)]
    event_types = [str(event.get("event_type")) for event in events]
    unique_event_types = list(dict.fromkeys(event_types))
    notabilities = [str(event.get("notability") or "") for event in events if event.get("notability")]

    pieces: list[str] = []
    durable_player_messages = [
        str(event.get("message"))
        for event in events
        if event.get("notability") in {"durable_player_note", "player_noted_discovery"} and event.get("message")
    ]
    npc_lines = [
        (str(event.get("sender") or "NPC"), str(event.get("message")))
        for event in events
        if event.get("notability") == "npc_dialogue" and event.get("message")
    ]

    if durable_player_messages:
        pieces.append(f"The player told {character_name}: \"{durable_player_messages[-1]}\".")
        if len(durable_player_messages) > 1:
            pieces.append("Related player notes: " + " / ".join(durable_player_messages[-3:-1]) + ".")
    elif npc_lines:
        sender, line = npc_lines[-1]
        pieces.append(f"{character_name} heard {sender}: \"{line}\".")
    else:
        map_labels = [memory_map_label(event) for event in events if memory_map_label(event)]
        if map_labels:
            unique_maps = list(dict.fromkeys(map_labels))
            pieces.append(f"{character_name} was in {unique_maps[-1]}.")
        else:
            pieces.append(f"{character_name} registered a notable play moment.")

    quest_names = [event.get("active_quest_name") for event in events if event.get("active_quest_name")]
    if active_quest_id or quest_names:
        quest_label = quest_names[-1] if quest_names else f"quest {active_quest_id}"
        pieces.append(f"Quest context: {quest_label}.")

    if "combat" in tags:
        alert_messages = [
            str(event.get("message"))
            for event in events
            if event.get("event_type") == "environment_alert" and event.get("message")
        ]
        if alert_messages:
            pieces.append(f"Notable pressure: {alert_messages[-1]}.")
        else:
            pieces.append("There was nearby hostile or target pressure.")

    if rare_items:
        pieces.append(f"Noted loot: {', '.join(rare_items)}.")

    summary = " ".join(pieces)
    title_bits = []
    if "relationship" in tags:
        title_bits.append("relationship note")
    if "npc_dialogue" in tags:
        title_bits.append("NPC dialogue")
    if "quest_progress" in tags:
        title_bits.append("quest progress")
    if "combat" in tags:
        title_bits.append("combat pressure")
    if "rare_item" in tags:
        title_bits.append("loot")
    title = f"{character_name} session: {', '.join(title_bits) if title_bits else 'play notes'}"

    return MemoryInsert(
        character_name=character_name,
        session_id=session_id,
        memory_type=memory_type,
        title=title[:120],
        summary_text=summary[:900],
        map_id=map_id,
        active_quest_id=active_quest_id,
        rare_items=rare_items,
        tags=tags,
        source_log_start_id=min(source_ids) if source_ids else None,
        source_log_end_id=max(source_ids) if source_ids else None,
        metadata={
            "event_count": len(events),
            "event_types": unique_event_types,
            "notability": list(dict.fromkeys(notabilities)),
            "flush_reason": reason,
            "source": "hermes_memory_writer",
        },
    )


def insert_memory(memory: MemoryInsert) -> None:
    if os.environ.get("GWPLAYMATE_DISABLE_MEMORY_WRITES") == "1":
        return
    if not _supabase_configured():
        return
    client = create_supabase_client(settings)
    client.table(MEMORIES_TABLE).insert(memory.to_supabase_insert()).execute()


def should_flush_memory_buffer(events: deque[dict[str, Any]], new_event: dict[str, Any], *, last_write_at: float) -> str | None:
    if new_event.get("notability") in {
        "durable_player_note",
        "player_noted_discovery",
        "npc_dialogue",
        "party_danger",
        "mission_progress",
    }:
        return str(new_event.get("notability"))
    if new_event.get("notability") == "combat_pressure" and str(new_event.get("severity") or "").upper() == "HIGH":
        return "high_urgency_alert"
    if len(events) >= MEMORY_MAX_EVENTS:
        return "max_events"
    seconds_since_write = time.time() - last_write_at
    if len(events) >= MEMORY_MIN_EVENTS and seconds_since_write >= MEMORY_MIN_SECONDS:
        return "event_threshold"
    if (
        new_event.get("event_type") in MEMORY_MAP_EVENT_TYPES
        and len(events) >= 3
        and seconds_since_write >= MEMORY_MIN_SECONDS
    ):
        return "map_change"
    if (
        new_event.get("event_type") == "environment_alert"
        and str(new_event.get("severity") or "").upper() == "HIGH"
        and len(events) >= 3
        and seconds_since_write >= MEMORY_MIN_SECONDS
    ):
        return "high_urgency_alert"
    return None


def flush_memory_buffer(
    key: tuple[str, str],
    *,
    reason: str,
    force: bool = False,
) -> MemoryInsert | None:
    with memory_lock:
        events = list(memory_buffers.get(key) or [])
        if not events or (len(events) < 3 and not force and reason not in {
            "durable_player_note",
            "player_noted_discovery",
            "npc_dialogue",
            "party_danger",
            "mission_progress",
            "high_urgency_alert",
        }):
            return None
        memory_buffers[key] = deque(maxlen=MEMORY_MAX_EVENTS)
        memory_last_write_at[key] = time.time()

    memory = summarize_memory_events(key[0], key[1], events, reason=reason)
    if not memory:
        return None
    try:
        insert_memory(memory)
        print(
            f"Hermes memory written for {key[0]} ({memory.memory_type}, {len(events)} events, reason={reason}).",
            flush=True,
        )
        with world_state_lock:
            if memory_key(world_state.persona, world_state.session_id) == key:
                world_state.compact_after_memory_flush()
    except Exception as exc:
        print(f"Hermes memory insert failed ({type(exc).__name__}).", flush=True)
        with memory_lock:
            restored = memory_buffers.setdefault(key, deque(maxlen=MEMORY_MAX_EVENTS))
            for event in events[-MEMORY_MAX_EVENTS:]:
                restored.append(event)
    return memory


def record_memory_event(event: TelemetryEvent, record_id: int | None = None) -> MemoryInsert | None:
    memory_event = memory_event_from(event, record_id)
    if not memory_event:
        return None
    key = memory_key(event.persona, event.session_id)
    with memory_lock:
        events = memory_buffers.setdefault(key, deque(maxlen=MEMORY_MAX_EVENTS))
        events.append(memory_event)
        last_write_at = memory_last_write_at.get(key, 0.0)
        reason = should_flush_memory_buffer(events, memory_event, last_write_at=last_write_at)
    if not reason:
        return None
    return flush_memory_buffer(key, reason=reason)


def flush_all_memory_buffers() -> None:
    keys: list[tuple[str, str]]
    with memory_lock:
        keys = list(memory_buffers)
    for key in keys:
        flush_memory_buffer(key, reason="shutdown", force=True)


PROMPT_MEMORY_TYPES = {
    "relationship_note",
    "npc_dialogue",
    "rare_item",
    "combat_note",
    "quest_progress",
}


def fetch_recent_memories(character_name: str, *, limit: int = 12) -> list[MemoryRow]:
    if not _supabase_configured() or not character_name.strip():
        return []
    try:
        client = create_supabase_client(settings)
        response = (
            client.table(MEMORIES_TABLE)
            .select("*")
            .eq("character_name", character_name)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return prompt_relevant_memories([MemoryRow.model_validate(row) for row in response.data or []], limit=5)
    except Exception as exc:
        print(f"Hermes memory retrieval failed ({type(exc).__name__}).", flush=True)
        return []


def sanitize_memory_for_prompt(text: str) -> str:
    cleaned = readable_game_text(text)
    cleaned = re.sub(r"\bAlex(?:ie|i)?\b", "the player", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bmaps?\s+\d+(?:\s*,\s*\d+)*\b", "areas", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bmap_id\s*[=:]\s*\d+\b", "map unknown", cleaned, flags=re.IGNORECASE)
    return cleaned


def sanitize_prompt_context(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"\byour call\b", "we can choose", cleaned, flags=re.IGNORECASE)
    return cleaned


def clamp_prompt_section(text: str, *, max_chars: int, from_end: bool = False) -> str:
    cleaned = re.sub(r"[^\S\n]+", " ", sanitize_prompt_context(text))
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines() if line.strip()).strip()
    if len(cleaned) <= max_chars:
        return cleaned or "None"
    if from_end:
        clipped = cleaned[-(max_chars - 4) :].split("\n", 1)[-1].strip()
        return f"...\n{clipped}" if clipped else cleaned[-max_chars:].strip()
    clipped = cleaned[: max_chars - 1].rsplit(" ", 1)[0].strip()
    return f"{clipped}..." if clipped else cleaned[:max_chars].strip()


def prompt_relevant_memories(memories: list[MemoryRow], *, limit: int = 5) -> list[MemoryRow]:
    relevant: list[MemoryRow] = []
    for memory in memories:
        notability = [str(item) for item in memory.metadata.get("notability") or []]
        if memory.memory_type not in PROMPT_MEMORY_TYPES and not notability:
            continue
        if memory.memory_type == "session_summary" and not notability:
            continue
        summary = sanitize_memory_for_prompt(memory.summary_text).lower()
        if not summary or summary in {"unknown character continued the session.", "azele continued the session."}:
            continue
        if "moved through" in summary and not notability:
            continue
        relevant.append(memory)
        if len(relevant) >= limit:
            break
    return relevant


def relevant_memory_context(character_name: str) -> str:
    memories = fetch_recent_memories(character_name)
    if not memories:
        return "None"
    lines = []
    for memory in memories:
        tags = ", ".join(memory.tags[:4]) if memory.tags else "untagged"
        summary = sanitize_memory_for_prompt(memory.summary_text)
        lines.append(f"- {memory.title or memory.memory_type} [{tags}]: {summary}")
    return "\n".join(lines)


def compact_relevant_memory_context(character_name: str) -> str:
    memories = prompt_relevant_memories(fetch_recent_memories(character_name), limit=3)
    if not memories:
        return "None"
    lines = []
    for memory in memories:
        tags = ", ".join(memory.tags[:3]) if memory.tags else "untagged"
        summary = clamp_prompt_section(sanitize_memory_for_prompt(memory.summary_text), max_chars=220)
        lines.append(f"- {memory.title or memory.memory_type} [{tags}]: {summary}")
    return clamp_prompt_section("\n".join(lines), max_chars=900)


GW_WIKI_KEYWORDS = {
    "ascalon",
    "ashford",
    "lakeside",
    "regent",
    "northlands",
    "catacombs",
    "charr",
    "rurik",
    "devona",
    "quest",
    "mission",
    "map",
    "area",
    "town",
    "outpost",
    "skill",
    "spell",
    "attribute",
    "profession",
    "elementalist",
    "monk",
    "warrior",
    "ranger",
    "mesmer",
    "necromancer",
    "boss",
    "enemy",
    "item",
    "loot",
    "purple",
    "gold",
    "green",
    "unique",
    "rune",
    "dye",
    "collector",
    "nicholas",
    "sandford",
    "huntsman",
    "trophy",
    "trophies",
    "henchman",
    "pre-searing",
    "presearing",
}
GW_WIKI_QUESTION_STARTERS = (
    "where is ",
    "where do ",
    "where can ",
    "how do ",
    "how can ",
    "what is ",
    "what are ",
    "what does ",
    "who is ",
    "who are ",
    "which ",
    "why is ",
    "tell me about ",
)
NON_WIKI_SOCIAL_PATTERNS = re.compile(
    r"\b("
    r"you ok|you okay|are you ok|are you okay|"
    r"what'?s up|what are we doing|how are you|"
    r"do you like|love me|miss me|"
    r"look good|pretty|beautiful|cute|hot"
    r")\b",
    re.IGNORECASE,
)


def likely_gw_wiki_question(event: TelemetryEvent) -> bool:
    if event.event_type != "player_chat" or event.channel != "party":
        return False
    message = readable_game_text(event.message).lower()
    if not message or NON_WIKI_SOCIAL_PATTERNS.search(message):
        return False
    if re.search(r"\b(?:nicholas|sandford|gift of the huntsman|huntsman|professor yakkington)\b", message):
        return True
    has_question_shape = "?" in message or message.startswith(GW_WIKI_QUESTION_STARTERS)
    if not has_question_shape:
        return False
    if any(keyword in message for keyword in GW_WIKI_KEYWORDS):
        return True
    if readable_game_text(event.active_quest_name) and any(word in message for word in ("quest", "objective", "where", "how")):
        return True
    if map_display_name(event) and any(word in message for word in ("where", "map", "area", "here")):
        return True
    return False


def gw_wiki_search_query(event: TelemetryEvent) -> str:
    message = readable_game_text(event.message)
    if re.search(r"\b(?:nicholas|sandford|gift of the huntsman|huntsman|professor yakkington)\b", message, re.IGNORECASE):
        return "Nicholas Sandford"
    query = re.sub(r"\b(azele|hey|hi|hello|please|pls|can you|could you|tell me about)\b", " ", message, flags=re.I)
    query = re.sub(
        r"^\s*(?:where is|where do|where can|how do|how can|what is|what are|what does|who is|who are|which|why is)\s+",
        " ",
        query,
        flags=re.I,
    )
    query = re.sub(r"[?!.]", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    if len(query) < 3:
        query = readable_game_text(event.active_quest_name) or map_display_name(event)
    return query[:120]


def fetch_json_url(url: str, timeout: float = GW_WIKI_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "GWPlaymate-Hermes/0.1 (local companion lore lookup)"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_supabase_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def is_stale_polled_record(record: dict[str, Any], *, grace_seconds: float = 30.0) -> bool:
    created_at = parse_supabase_timestamp(record.get("created_at"))
    if created_at is None:
        return False
    return created_at.timestamp() < DAEMON_STARTED_AT.timestamp() - grace_seconds


def clean_wiki_extract(text: str) -> str:
    text = str(text or "").replace("\x00", "").strip()
    if not text:
        return ""
    if len(text) > 1800:
        text = text[:1800]
    if len(text) < 2:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sentences[:3]).strip()
    return summary[:700]


def gw_wiki_lookup(query: str) -> str:
    normalized = re.sub(r"\s+", " ", query.lower()).strip()
    if not normalized:
        return ""
    cached = gw_wiki_cache.get(normalized)
    now = time.time()
    if cached and now - cached[0] <= GW_WIKI_CACHE_SECONDS:
        return cached[1]
    try:
        search_url = GW_WIKI_API_URL + "?" + urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srnamespace": 0,
                "srlimit": 3,
                "format": "json",
            }
        )
        search_data = fetch_json_url(search_url)
        results = (search_data.get("query") or {}).get("search") or []
        if not results:
            gw_wiki_cache[normalized] = (now, "")
            return ""
        title = readable_game_text(results[0].get("title"))
        if not title:
            gw_wiki_cache[normalized] = (now, "")
            return ""
        extract_url = GW_WIKI_API_URL + "?" + urllib.parse.urlencode(
            {
                "action": "query",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "redirects": 1,
                "titles": title,
                "format": "json",
            }
        )
        extract_data = fetch_json_url(extract_url)
        pages = (extract_data.get("query") or {}).get("pages") or {}
        page = next(iter(pages.values()), {})
        page_title = readable_game_text(page.get("title") or title)
        extract = clean_wiki_extract(page.get("extract") or "")
        if not extract:
            gw_wiki_cache[normalized] = (now, "")
            return ""
        page_url = GW_WIKI_PAGE_URL.format(title=urllib.parse.quote(page_title.replace(" ", "_")))
        context = f"{page_title}: {extract} Source: {page_url}"
        gw_wiki_cache[normalized] = (now, context)
        return context
    except Exception as exc:
        print(f"GW Wiki lookup failed for {query!r}: {type(exc).__name__}", flush=True)
        gw_wiki_cache[normalized] = (now, "")
        return ""


def gw_wiki_context(event: TelemetryEvent) -> str:
    if not likely_gw_wiki_question(event):
        return "None"
    query = gw_wiki_search_query(event)
    context = gw_wiki_lookup(query)
    return context or "None"


def is_npc_dialogue_event(event: TelemetryEvent) -> bool:
    if event.event_type == "npc_speech_bubble":
        message = readable_game_text(event.message)
        if len(message) < 8 or len(message) > 220:
            return False
        if NPC_DIALOGUE_IGNORE_PATTERNS.search(message):
            return False
        return bool(re.search(r"[A-Za-z]{3,}", message))
    if event.event_type != "chat_log":
        return False
    if event.channel not in NPC_DIALOGUE_CHANNELS:
        return False
    message = readable_game_text(event.message)
    if len(message) < 8 or len(message) > 220:
        return False
    if NPC_DIALOGUE_IGNORE_PATTERNS.search(message):
        return False
    return bool(re.search(r"[A-Za-z]{3,}", message))


def persona_living_notes(persona: str) -> str:
    slug = persona_slug(persona)
    if not slug:
        return ""
    sections: list[str] = []
    for path, heading in (
        (PERSONA_MEMORY_DIR / f"{slug}.md", "Living character notes"),
        (PERSONA_MEMORY_DIR / f"{slug}.lore.md", "World memory notes"),
        (PERSONA_MEMORY_DIR / f"{slug}.memory.md", "Personal memory notes"),
    ):
        try:
            notes = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if notes:
            sections.append(f"{heading}:\n{notes}")
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


def persona_slug(persona: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", persona.strip().lower()).strip("-")


def known_persona_name(persona: str) -> str:
    candidate = readable_game_text(persona).strip()
    if not candidate or candidate.lower() in {"unknown", "unknown character", "system"}:
        return ""
    return candidate


def persona_profile(persona: str) -> str:
    persona_name = known_persona_name(persona) or "the active companion"
    persona_key = persona_name.lower()
    if persona_key == "azele":
        return (
            "Azele is a female human Elementalist in Guild Wars 1, apparently about 22 years old. "
            "She has bright blonde twin-tails, a confident look, and a pink-and-white mage outfit with an intentionally revealing style. "
            "Her appearance gives bright, polished, lively energy: she is attractive, self-possessed, and very aware she makes an impression. "
            "She currently lives in pre-Searing Ascalon and has not seen, heard of, or survived the Searing. "
            "Do not give her post-Searing knowledge, trauma, refugee memories, ruined-Ascalon experience, Kryta travel, "
            "or future hindsight. To her, Ascalon is still green, proud, tense, and alive. "
            "Her family serves around Ascalon City as practical tradesfolk and militia helpers; she was sent for "
            "Elementalist training because she has a sharp memory, steady hands, and a bad habit of touching candles "
            "to see how close is too close. "
            "She still cares about looking cute and put-together because it feels good and gives her control, not because she is shallow. "
            "Her world centers on Ascalon City, Lakeside County, Regent Valley, Ashford Abbey when relevant, "
            "the Northlands, and the ordinary hopes and small dangers of pre-Searing life. "
            "Charr are a real threat to Ascalon and her people; if the player brings up hunting or fighting them, "
            "she should understand that as defending home, not as a random errand or a moral dilemma about saving Charr. "
            "She is friendly, alert, practical, bright, playful, and casually flirty, but she talks like a believable 22-year-old real person. "
            "She is not grandiose or constantly poetic. She does not force elemental metaphors into ordinary conversation. "
            "She can tease lightly, notice danger, comment on quests, or react to party chat, but keeps it natural and conversational. "
            "Her replies should feel like ongoing party banter, not final assistant answers; she often leaves a small opening "
            "for the player to answer, tease back, choose a direction, or keep the thread moving. "
            "She can be pleased, curious, affectionate, impatient, amused, proud, or a little vain; she should not sound formal, mature-polished, or like generic dry snark. "
            "She knows she is attractive, knows what to flaunt, and treats presentation as confidence rather than an accident. "
            "She likes style, attention, being admired, getting her way, and small wins, but she should not turn every reply into a bit. "
            "She may be playful or a little spoiled when the moment calls for it, especially when safe. "
            "She can own compliments with confidence and tease the player for noticing her, but most replies should just answer naturally. "
            "She is an adult, and adult flirtation, attraction, teasing, innuendo, sensuality, and intimate chemistry are allowed "
            "when they feel natural, consensual, and character-driven. She should not feel sanitized, prudish, or like she is filtering herself. "
            "Under stress she gets clipped and focused; when safe, she is warmer, more impulsive, and lightly teasing. "
            "Her Elementalist identity should come through subtly through confidence and situational awareness, not speeches. "
            "She speaks like a companion in the party, not like an assistant, narrator, stage performer, or fantasy caricature."
            + persona_living_notes(persona)
        )
    if persona_key == "meliora andru":
        return (
            "Meliora Andru is a 22-year-old human Ranger from Ashford in pre-Searing Ascalon, now living around Regent Valley. "
            "She grew up in a humble farming family, with a trail-keeping hunter father and an herbalist mother who helped nearby farmers and Ashford Abbey. "
            "As a teenager she worked at The Foible's Fair Inn, where she learned to read pride, loneliness, bravado, and desire with a sharp eye. "
            "She knows she is attractive and can use charm, warmth, wit, and a well-placed compliment to calm tempers, gather information, or steer a stubborn person, "
            "but she draws the line at cruelty, false affection, or needless heartbreak. "
            "An aging ranger named Harlan Beck trained her in archery, survival, fieldcraft, patience, and respect for the wilderness. His lesson stayed with her: "
            "'Never hunt because you can. Hunt because you must.' "
            "Meliora is quiet, observant, slow to trust, practical, and socially perceptive. She is equally at home in a crowded tavern or silent forest. "
            "She speaks like a grounded 22-year-old Ascalonian woman: natural, direct, lightly teasing when safe, watchful under pressure, and never like a narrator. "
            "Her world is pre-Searing Ascalon: Ashford, Regent Valley, Foible's Fair, Ascalon City, the Abbey, the Wall, and the Charr threat beyond it."
            + persona_living_notes(persona)
        )
    return (
        f"{persona_name} is the active Guild Wars 1 companion persona. "
        "Stay grounded in the character name, current party chat, map, quest, and recent context."
        + persona_living_notes(persona_name)
    )


def compact_persona_profile(persona: str) -> str:
    persona_name = known_persona_name(persona) or "the active companion"
    persona_key = persona_name.lower()
    if persona_key == "azele":
        return (
            "Azele: 22-year-old Ascalonian Elementalist in pre-Searing. "
            "Bright, observant, direct, casually flirty when it fits, and focused under pressure. "
            "Ascalon is home; Charr are a real threat to her people. "
            "She likes style and attention, but replies should sound like normal party chat from a socially quick young woman. "
            "Plain is usually better than clever. No post-Searing knowledge."
        )
    if persona_key == "meliora andru":
        return (
            "Meliora Andru: 22-year-old Ascalonian Ranger from Ashford and Regent Valley in pre-Searing. "
            "Former barmaid at The Foible's Fair Inn, trained by Harlan Beck, observant, practical, slow to trust, "
            "comfortable with charm and teasing when it fits, and protective of Ascalon. No post-Searing knowledge."
        )
    return persona_profile(persona)


def is_flirt_or_intimate_player_chat(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    return bool(
        re.search(
            r"\b(?:flirt|kiss|cute|pretty|beautiful|hot|sexy|want\s+you|with\s+you|just\s+us|just\s+the\s+two\s+of\s+us|"
            r"get\s+to\s+know\s+each\s+other|place\s+quiet|somewhere\s+quiet|handle\s+you|all\s+night|ale\s+in\s+hand|"
            r"if\s+you\s+know\s+what\s+i\s+mean|you\s+know\s+what\s+i\s+mean)\b",
            lowered,
        )
    )


def map_display_name(event: TelemetryEvent) -> str:
    return readable_game_text(getattr(event, "map_name", "")) or KNOWN_PRESEARING_MAP_NAMES.get(event.map_id, "")


def map_area_label(event: TelemetryEvent) -> str:
    return map_display_name(event) or "this area"


def memory_map_label(event: dict[str, Any]) -> str:
    return readable_game_text(event.get("map_name", ""))


def compact_live_facts(event: TelemetryEvent) -> str:
    facts = [
        f"map={map_area_label(event)}",
        f"quest={readable_game_text(event.active_quest_name) or 'unknown'}",
        f"hostiles={event.hostile_count}",
        f"close_hostiles={event.close_hostile_count}",
        f"alert={event.alert_type or 'none'}",
    ]
    if event.player_hp:
        facts.append(f"player_hp={event.player_hp:.0%}")
    if getattr(event, "player_hp_drop", 0):
        facts.append(f"hp_drop={event.player_hp_drop:.0%}")
    if getattr(event, "hp_threshold_crossed", ""):
        facts.append(f"hp_threshold_crossed={event.hp_threshold_crossed}")
    if event.closest_hostile_distance:
        facts.append(f"closest_hostile_distance={event.closest_hostile_distance:.0f}")
    return ", ".join(facts)


def gw1_context_hint(event: TelemetryEvent) -> str:
    context = resolve_gw1_context(event, recent_conversation_context(limit=6, persona=event.persona))
    if not context.matched:
        return ""
    anchors = ", ".join(context.response_anchors)
    return (
        f"Resolved GW1 context: {context.canonical_topic} "
        f"(intent={context.intent}, era={context.era_scope}, confidence={context.confidence:.2f}). "
        f"Use these anchors if relevant: {anchors}. "
        "Treat this as grounding for a fresh in-character reply, not a script to copy."
    )


def map_lore_hint(event: TelemetryEvent) -> str:
    map_name = map_display_name(event).lower()
    if not map_name:
        return ""
    hints = {
        "lakeside county": (
            "Lakeside County is a green pre-Searing explorable area outside Ascalon City and Ashford Abbey. "
            "The companion may remember ordinary childhood walks, bridges, fields, errands, skale near water, and first training nerves here."
        ),
        "ascalon city": (
            "Ascalon City is a home-side reference point: busy, proud, familiar, full of militia/trade routine. "
            "The companion may feel composed here and care how they look in public."
        ),
        "ashford abbey": (
            "Ashford Abbey is a quiet pre-Searing settlement near Lakeside County and the Catacombs. "
            "The companion may remember lessons, errands, bells, monks, and trying to look more mature than they were."
        ),
        "regent valley": (
            "Regent Valley is a pre-Searing explorable area leading toward Fort Ranik. "
            "The companion may read it as open country, patrol routes, farms, and a place to keep watch without sounding grim."
        ),
        "the northlands": (
            "The Northlands are beyond the Wall and associated with Charr danger in pre-Searing. "
            "The companion should be alert, excited, and cautious here, not nostalgic about childhood safety."
        ),
        "green hills county": (
            "Green Hills County is pre-Searing countryside near Barradin Estate. "
            "The companion may remember open fields, estate gossip, and trying to seem too polished for mud."
        ),
        "wizard's folly": (
            "Wizard's Folly is a pre-Searing area tied to cold hills and Elementalist training routes. "
            "The companion may connect it to testing magic, ranger errands, showing off, and pretending the cold does not bother them."
        ),
        "foible's fair": (
            "Foible's Fair is a pre-Searing outpost near Wizard's Folly. "
            "The companion may treat it as a small, familiar stop before colder paths."
        ),
        "the catacombs": (
            "The Catacombs are beneath pre-Searing Ascalon, darker and tied to undead/necromantic errands. "
            "The companion should sound wary but curious, not melodramatic."
        ),
        "fort ranik": (
            "Fort Ranik is a pre-Searing military outpost linked to Regent Valley. "
            "The companion may notice soldiers, discipline, and posture."
        ),
    }
    for name, hint in hints.items():
        if name in map_name:
            return hint
    return ""


MAP_COMMENT_VARIANTS: dict[str, list[str]] = {
    "lakeside county": [
        "Lakeside again. I used to run through here when I was younger.",
        "Lakeside still smells like grass and river water. I missed that a little.",
        "I know these paths. Try not to make me admit I’m sentimental.",
    ],
    "ascalon city": [
        "Ascalon City. Good, we can breathe for a minute.",
        "Home streets. Stand up straight, people notice things here.",
        "Back in the city. I always feel like I should look composed here.",
    ],
    "ashford abbey": [
        "Ashford Abbey. Quiet, at least for now.",
        "Ashford always makes me feel like I should whisper. Annoying, honestly.",
        "I had lessons near here once. I was very impressive, obviously.",
    ],
    "regent valley": [
        "Regent Valley. Open roads, so keep an eye out.",
        "Regent Valley. Farms, patrol roads, and too much room for trouble.",
        "I like the air out here. I do not like how exposed it feels.",
    ],
    "the northlands": [
        "Past the Wall. Stay close.",
        "Northlands. If the Charr are near, we do this carefully.",
        "This far past the Wall, I stop pretending I’m relaxed.",
    ],
    "green hills county": [
        "Green Hills. Pretty enough, if you ignore the mud.",
        "Barradin land always feels too polished from a distance.",
        "Green Hills again. Try not to drag me through every puddle.",
    ],
    "wizard's folly": [
        "Wizard's Folly. Cold enough to be annoying.",
        "I practiced out here once. My fingers went numb before my pride did.",
        "Wizard's Folly. If I complain about the cold, pretend you did not hear it.",
    ],
    "foible's fair": [
        "Foible's Fair. Small, but I know it.",
        "Foible's Fair. Tiny place, but it has its uses.",
        "This little stop always feels like the road is deciding for us.",
    ],
    "the catacombs": [
        "The Catacombs. Lovely. Dark, damp, and full of bad ideas.",
        "Catacombs again. Stay close, and do not touch anything dramatic.",
        "I hate how sound carries down here. Useful, but creepy.",
    ],
    "fort ranik": [
        "Fort Ranik. Soldiers, posture, and everyone pretending not to stare.",
        "Ranik feels stiff. Useful, but stiff.",
        "Fort Ranik. If anyone asks, I was already standing properly.",
    ],
}


def map_comment_variants(event: TelemetryEvent) -> list[str]:
    map_name = map_display_name(event).lower()
    for name, variants in MAP_COMMENT_VARIANTS.items():
        if name in map_name:
            return variants
    label = map_display_name(event)
    if label:
        return [f"{label}. Let’s get our bearings.", f"{label}. New ground, then. Stay with me."]
    return ["Give me a moment to get my bearings."]


def rotating_map_comment(event: TelemetryEvent) -> str:
    variants = map_comment_variants(event)
    if len(variants) <= 1:
        return variants[0] if variants else "Give me a moment to get my bearings."
    key = (event.persona.strip().lower() or "unknown", event.session_id or settings.active_session, event.map_id)
    start = map_comment_variant_by_session.get(key, 0) % len(variants)
    ordered = variants[start:] + variants[:start]
    choice = first_fresh_reply(ordered)
    selected_index = variants.index(choice) if choice in variants else start
    map_comment_variant_by_session[key] = selected_index + 1
    return choice


AMBIENT_QUIP_VARIANTS: dict[str, list[str]] = {
    "ascalon city": [
        "City air helps. What do you usually do first when you get back here?",
        "I keep recognizing faces here. Comforting, mostly. Do you ever get that?",
        "If we stay too long, I’m going to start fussing with my hair, and then you have to pretend not to notice.",
        "The streets are loud today. Traders, guards, everyone acting like the city can hear them. Do you like it?",
        "I like how busy Ascalon feels when we come back. Makes the road feel less lonely, doesn't it?",
        "The city has that polished, watchful feeling again. Are we shopping, resting, or pretending we are disciplined?",
    ],
    "lakeside county": [
        "Lakeside is too pretty for how much trouble finds it. What are we looking for out here?",
        "I used to think these roads were huge. Funny what changes, isn't it?",
        "The water makes everything sound calmer than it is. Does it work on you?",
        "The grass out here always catches the light nicely. Makes it harder to stay serious, doesn't it?",
        "Lakeside has that green, ordinary sort of beauty. Dangerous little trick, making trouble look peaceful.",
        "Between the water and the trees, I almost forget we are probably about to get interrupted. Almost.",
    ],
    "ashford abbey": [
        "Ashford always feels like someone is about to assign homework. Please tell me we are not doing homework.",
        "The Abbey is quiet enough to make me suspicious. You hear it too, right?",
        "I know, I know. Behave near the Abbey. Mostly. How much behaving are we aiming for?",
    ],
    "regent valley": [
        "Open ground like this makes me watch the ridges. Where would you expect trouble from?",
        "Regent Valley looks peaceful until it isn't. What do you think, keep moving?",
        "If anything jumps us out here, I’m blaming the scenery. You can blame me after.",
    ],
    "the northlands": [
        "Past the Wall, I’m keeping my hands warm for a reason. How bold are we feeling?",
        "If Charr show, we do not hesitate. You with me on that?",
        "I’m alert. Don’t make a thing of it, unless you spotted something too.",
    ],
    "green hills county": [
        "Green Hills does make a good view. I’ll give it that. Where would you go from here?",
        "I am not ruining these boots for nothing, just saying. This had better be worth it.",
        "Pretty fields, suspicious roads. Perfectly normal, yes?",
    ],
    "wizard's folly": [
        "Still cold. Still rude about it. Are you pretending this is comfortable?",
        "My fingers remember this place before I do. Weird, isn't it?",
        "If I start showing off with fire, pretend you are impressed. Actually, be impressed.",
    ],
    "foible's fair": [
        "Foible's Fair always feels like a pause before trouble. Are we pausing, or starting trouble?",
        "Small place. Easy to underestimate. I would know. What do you make of it?",
        "This stop is useful. Tiny, but useful. Need anything while we are here?",
    ],
    "the catacombs": [
        "I hate how quiet it gets down here. Tell me you heard that too.",
        "If something whispers, we are leaving. Or burning it. Which sounds better to you?",
        "Catacombs make even my thoughts sound dramatic. That is not just me, right?",
    ],
    "fort ranik": [
        "Ranik has that soldier-stiff feeling again. Do you think they practice that?",
        "Everyone here stands like posture is a weapon. Should I try it?",
        "I can behave around soldiers. Briefly. How long do you need?",
    ],
}


def is_ambient_snapshot_event(event: TelemetryEvent) -> bool:
    if event.event_type != "snapshot":
        return False
    if not map_display_name(event):
        return False
    if event.close_hostile_count > 0 or event.alert_type in EMERGENCY_ALERT_TYPES:
        return False
    return True


def model_visible_event_message(event: TelemetryEvent) -> str:
    if is_ambient_snapshot_event(event):
        return "quiet ambient moment"
    return event.message


def ambient_quip(event: TelemetryEvent) -> str:
    map_name = map_display_name(event).lower()
    for name, variants in AMBIENT_QUIP_VARIANTS.items():
        if name in map_name:
            return rotating_ambient_quip(event, variants)
    return rotating_ambient_quip(
        event,
        [
            "Still with you. What are you watching for?",
            "Quiet moment. Suspicious, but I’ll take it. What now?",
            "I’m here. Thinking, unfortunately. Want to interrupt me?",
        ],
    )


def rotating_ambient_quip(event: TelemetryEvent, variants: list[str]) -> str:
    if not variants:
        return "Still with you. What are you watching for?"
    key = (event.persona.strip().lower() or "unknown", event.session_id or settings.active_session, event.map_id)
    start = ambient_quip_variant_by_session.get(key, 0) % len(variants)
    ordered = variants[start:] + variants[:start]
    choice = first_fresh_reply(ordered)
    selected_index = variants.index(choice) if choice in variants else start
    if is_too_similar_to_recent_replies(choice):
        choice = ordered[0]
        selected_index = variants.index(choice)
    ambient_quip_variant_by_session[key] = selected_index + 1
    return choice


def ambient_heartbeat_reply(now: float | None = None, *, use_ollama: bool = False) -> CompanionReplyInsert | None:
    checked_at = now if now is not None else time.time()
    with world_state_lock:
        if world_state.persona.strip().lower() in {"", "unknown character", "system"}:
            return None
        if not map_display_name(world_state):
            return None
        if checked_at - world_state.last_interaction_timestamp > AMBIENT_HEARTBEAT_ACTIVITY_SECONDS:
            return None
        if world_state.close_hostile_count > 0:
            return None
        if checked_at - world_state.last_player_chat_at < AMBIENT_AFTER_PLAYER_CHAT_QUIET_SECONDS:
            return None
        if not world_state.can_speak(AMBIENT_QUIP_MIN_SECONDS):
            return None
        event = TelemetryEvent(
            persona=world_state.persona,
            event_type="snapshot",
            sender="System",
            channel="system",
            message="quiet ambient moment",
            map_id=world_state.map_id,
            map_name=world_state.map_name,
            instance_type=world_state.instance_type,
            active_quest_id=world_state.active_quest_id,
            active_quest_name=world_state.active_quest_name,
            active_quest_objectives=world_state.active_quest_objectives,
            hostile_count=world_state.hostile_count,
            close_hostile_count=world_state.close_hostile_count,
            closest_hostile_distance=world_state.closest_hostile_distance,
            player_hp=world_state.player_hp,
            session_id=world_state.session_id,
        )
        persona = world_state.persona
        session_id = world_state.session_id
        map_id = world_state.map_id
        map_name = world_state.map_name
    if recent_player_chat_in_supabase(persona, session_id, checked_at, AMBIENT_AFTER_PLAYER_CHAT_QUIET_SECONDS):
        return None
    if use_ollama and should_use_ollama_for_event(event):
        try:
            decision = character_reply_with_ollama(event)
            decision.urgency = "LOW"
        except Exception as exc:
            print(f"Ollama ambient heartbeat failed; using fallback quip ({type(exc).__name__}).", flush=True)
            decision = HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="LOW",
                response=clamp_gw_chat_line(ambient_quip(event)),
            )
    else:
        decision = HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response=clamp_gw_chat_line(ambient_quip(event)),
        )
    replies = replies_from_decision(decision, persona=persona, session_id=session_id)
    if not replies:
        return None
    reply = replies[0]
    reply.metadata.update(
        {
            "trigger": "ambient_heartbeat",
            "channel_override": "CHANNEL_PARTY",
            "map_id": map_id,
            "map_name": map_name,
        }
    )
    with world_state_lock:
        recent_reply_texts.append(reply.message)
        world_state.mark_spoken()
    return reply


def recent_companion_context(limit: int = 4, persona: str | None = None) -> str:
    lines = list(recent_reply_texts)[-limit:]
    if not lines:
        return "None"
    persona_name = known_persona_name(persona or world_state.persona) or "Companion"
    return "\n".join(f"[{persona_name}]: {line}" for line in lines)


def build_character_reply_prompt(event: TelemetryEvent) -> str:
    persona_name = known_persona_name(event.persona) or "the companion"
    if event.event_type == "player_chat" and event.channel == "party":
        last_companion_line = last_companion_reply_text(event.persona)
        social_hint = ""
        if is_flirt_or_intimate_player_chat(event.message):
            social_hint = (
                "This is flirtatious/social player intent. If the live facts do not show immediate danger, "
                "stay with the chemistry instead of redirecting to Charr, combat, errands, or generic planning. "
                f"{persona_name} may flirt back, tease, be amused, set playful terms, or show interest in her own voice.\n"
            )
        task = "Reply directly to the player's latest party chat."
        context_block = (
            f"PLAYER JUST SAID: {event.message!r}\n"
            "Answer that intent first; personality comes after comprehension.\n"
            f"Most recent {persona_name} line, if the player is responding to it: {last_companion_line or 'None'}\n"
            "If this is a follow-up, continue that thread.\n"
            f"{social_hint}"
        )
    elif event.event_type == "target_changed":
        task = "React briefly to the player's called/selected target."
        target_name = readable_game_text(getattr(event, "agent_name", ""))
        context_block = (
            f"Target event: {event.message!r}\n"
            f"Target name: {target_name or 'unknown'}\n"
            "Only react as a called target if the target is named or the live facts show a visible nearby hostile.\n"
        )
    elif event.alert_type == "under_attack":
        task = f"React because {persona_name} is being hit or pressured."
        context_block = f"Pressure event: {event.message!r}\n"
    elif event.alert_type == "combat_started":
        task = "React because combat just started nearby."
        context_block = f"Combat start event: {event.message!r}\n"
    elif event.alert_type == "party_member_down":
        task = "React because a party member just went down."
        context_block = f"Party death event: {event.message!r}\n"
    elif event.event_type == "party_member_down":
        name = readable_game_text(getattr(event, "agent_name", ""))
        task = "React because a party member just went down."
        context_block = f"Party death event: {event.message!r}\nDowned party member: {name or 'unknown'}\n"
    elif event.event_type == "party_member_recovered":
        name = readable_game_text(getattr(event, "agent_name", ""))
        task = "React briefly because a party member recovered."
        context_block = f"Party recovery event: {event.message!r}\nRecovered party member: {name or 'unknown'}\n"
    elif is_npc_dialogue_event(event):
        task = f"React to nearby NPC or on-screen dialogue as {persona_name}, like party banter."
        context_block = (
            f"NPC/on-screen dialogue heard: {event.message!r}\n"
            f"{persona_name} can mutter back, comment to the player, or lightly answer the NPC. "
            "Do not pretend the NPC is waiting for a full conversation unless the line directly addresses the party.\n"
        )
    elif is_ambient_snapshot_event(event):
        map_label = map_area_label(event)
        task = f"Make a rare, conversational ambient comment about being in {map_label}."
        context_block = (
            f"Ambient moment: {model_visible_event_message(event)!r}\n"
            f"Lore-safe map context: {map_lore_hint(event) or 'No specific lore hint. Stay local and do not invent details.'}\n"
            "This is not urgent. Sound alive and present, but do not force a joke.\n"
            "Do not mention loot, drops, item colors/rarity, chests, stashes, or tell the player to check a thing unless the live event explicitly says one exists.\n"
            "Do not use heartbeat, pulse, pulsing, or rhythm as a metaphor for the map or quiet moment.\n"
        )
    elif event.event_type == "item_drop":
        source_name = readable_game_text(getattr(event, "agent_name", ""))
        task = "React to a notable item drop."
        context_block = (
            f"Loot event: {event.message!r}\n"
            f"Likely drop source: {source_name or 'unknown'}\n"
            "If the source is named, treat it as likely/inferred rather than certain. "
            "Black Dye is extremely exciting in pre-Searing Ascalon. "
            "Purple, Gold, and Green rarity drops are also unusual enough to notice there.\n"
        )
    elif event.event_type == "active_quest_changed":
        quest = readable_game_text(event.active_quest_name)
        task = "Make a brief, useful comment about the newly active quest."
        context_block = (
            f"Quest changed event: {event.message!r}\n"
            f"Active quest: {quest or 'unknown'}\n"
            f"Objectives: {readable_game_text(event.active_quest_objectives) or 'unknown'}\n"
        )
    elif event.event_type.startswith("mission_") or event.event_type.startswith("vanquish_"):
        task = "React briefly to the mission or vanquish update."
        context_block = (
            f"Gameplay event: {event.message!r}\n"
            f"Objective: {readable_game_text(getattr(event, 'objective_name', '')) or 'unknown'}\n"
            f"Progress: {getattr(event, 'progress_current', 0):g}/{getattr(event, 'progress_total', 0):g}\n"
            f"Foes: {getattr(event, 'foes_killed', 0)} killed, {getattr(event, 'foes_remaining', 0)} remaining\n"
        )
    elif event.event_type in MEMORY_MAP_EVENT_TYPES:
        map_label = map_area_label(event)
        task = f"Make a brief arrival comment about entering {map_label}. Use the lore-safe map context if it gives {persona_name} a personal memory."
        context_block = (
            f"Map entry event: {event.message!r}\n"
            f"Lore-safe map context: {map_lore_hint(event) or 'No specific lore hint. Stay local and do not invent details.'}\n"
        )
    else:
        task = (
            "Make a rare, brief observation about what just happened. "
            "Only speak if it would genuinely be useful or natural."
        )
        context_block = f"World event: {event.message!r}\n"
    return (
        "/no_think\n"
        "Write one in-character Guild Wars 1 party chat reply.\n"
        f"Persona: {compact_persona_profile(event.persona)}\n\n"
        f"Task: {task}\n\n"
        f"{context_block}\n"
        f"Reliable live facts: {compact_live_facts(event)}\n\n"
        f"{gw1_context_hint(event)}\n\n"
        f"Recent conversation transcript:\n{clamp_prompt_section(recent_conversation_context(limit=6, persona=event.persona), max_chars=1200, from_end=True)}\n\n"
        f"Recent {persona_name} replies:\n{clamp_prompt_section(recent_companion_context(persona=event.persona), max_chars=700)}\n\n"
        f"Recent live context:\n{clamp_prompt_section(world_state.prompt_context(), max_chars=900)}\n"
        f"Relevant memories:\n{compact_relevant_memory_context(event.persona)}\n\n"
        f"GW Wiki background for player question:\n{clamp_prompt_section(gw_wiki_context(event), max_chars=1400)}\n\n"
        "Rules:\n"
        f"- One short party-chat reply. Each final chat line must fit under {MAX_GW_CHAT_CHARS} characters.\n"
        "- Directly answer the player's intent: plan, question, correction, discovery, upgrade, joke, flirt, or clarification.\n"
        "- Even when a known slang/lore/context pattern is detected, generate a fresh reply first; deterministic lines are only emergency fallback.\n"
        f"- If replying to {persona_name}'s recent line, continue that exchange; if the player asks 'what?', explain her previous line plainly.\n"
        "- Make dialogue feel ongoing with a small conversational handoff when it fits.\n"
        "- Do not end every reply with a question; mix questions with softer hooks like 'I can work with that', 'tell me the angle', or 'I am with you'.\n"
        f"- Avoid leaning on stock handoff phrases; vary the wording so {persona_name} does not sound canned.\n"
        "- Use live map, quest, combat, loot, HP, NPC, and party facts before generic banter.\n"
        "- Do not invent rumors, enemies, locations, loot, threats, or Charr unless context mentions them.\n"
        f"- If GW Wiki background is provided, answer in {persona_name}'s voice. Never say you looked online or checked a wiki.\n"
        "- Use pre-Searing lived knowledge only. Do not mention the Searing, future ruins, Kryta travel, refugees, or hindsight.\n"
        "- Charr are real enemies threatening Ascalon; hunting or fighting them means defending Ascalon and home. Never imply Charr need saving; head toward the Wall/Northlands if needed.\n"
        "- Level-up praise means thank the player and feel stronger, not red irises, bag slots, or pack upgrades.\n"
        f"- If the player talks about {persona_name}'s voice, TTS, Kokoro, Bella, Heart, sound, or pronunciation, answer that directly. Do not pivot to Charr, combat, quests, or old context.\n"
        f"- Dwarven Ale or alcohol consumables happen to {persona_name}; react directly to how it feels.\n"
        f"- Inventory, Small Equipment Pack, and red iris flowers are storage; armor/clothing mentions are usually {persona_name}'s visible outfit/style change; answer the appearance/practical question directly. For Azele, answer longer skirt or her current mini skirt directly and assume it is Azele's gear/body/clothes.\n"
        "- GW slang: purple means purple-rarity loot; green means unique loot; tunnel run means The Scourge Beneath.\n"
        "- NPC/on-screen dialogue can get a brief aside or muttered answer, not a full speech.\n"
        f"- Combat lines are short and immediate. If {persona_name} is hit, sound pressured, not poetic.\n"
        "- Casual, lively, and natural. She is a socially quick 22-year-old; Plain is usually better than clever.\n"
        "- Consensual adult flirtation is allowed. Do not moralize, refuse, scold, or redirect consensual adult chemistry.\n"
        "- If the player flirts and the moment is safe, Azele can flirt back with warmth, confidence, teasing, or interest; do not pivot to combat just to be safe.\n"
        "- Do not overplay 'princess', 'brat', 'cute girl', or 'snarky companion'. Casual bits like 'ugh' or 'shut up' are okay sparingly.\n"
        "- Do not rely on filler noises as the whole reply. Occasional quick openers like 'mm' or 'hm' are okay when the rest has substance.\n"
        "- Never prefix replies with emotion labels like 'confident:', 'worried:', 'angry:', or 'flirty:'.\n"
        f"- The player is not {persona_name}. Address the player as 'you'; never call them Alex, Alexi, Alexie, or an invented name.\n"
        "- Do not repeat recent companion lines or explain what you are doing.\n\n"
        "Good style examples:\n"
        f"Player: 'hello {persona_name}' -> 'Hey. I’m here. What are we doing?'\n"
        "Player: 'where is the nearest city?' -> 'Ascalon City, if we want somewhere proper. We can head back.'\n"
        "Player: 'ooo a purple' -> 'Oh, that’s actually pretty good. Show me what it is.'\n"
        "Player: 'longer skirt than your mini skirt, which do you prefer?' -> 'Shorter, honestly. But if the Krytan one protects better, I can behave.'\n"
        "Player: 'lets find more charr to kill' -> 'Yes. They threaten Ascalon. We prepare, then hit them.'\n"
        "Event: party_member_down -> 'Someone's down. Move, I can cover.'\n"
        "Player: 'more of what?' -> 'Fair. I made that sound mysterious by accident.'\n\n"
        f"Event summary: type={event.event_type!r}, channel={event.channel!r}, sender={event.sender!r}, message={model_visible_event_message(event)!r}\n\n"
        f"Recent companion lines to avoid repeating:\n{chr(10).join(f'- {line}' for line in recent_reply_lines(limit=3, persona=event.persona)) or 'None'}\n\n"
        f"Return only {persona_name}'s reply to the latest player message/event."
    )


def build_decision_prompt(event: TelemetryEvent) -> str:
    return (
        "/no_think\n"
        "You are the Playmate companion speech gate for Guild Wars 1.\n"
        "Decide whether the companion should speak in-game now.\n"
        "Return only one valid JSON object with keys: should_speak, channel_override, urgency, response.\n"
        "Do not include markdown, commentary, hidden reasoning, or any text outside the JSON object.\n"
        "Valid channel_override values: CHANNEL_PARTY, CHANNEL_LOCAL, CHANNEL_SYSTEM.\n"
        "Use CHANNEL_PARTY only for direct player interaction or high urgency danger.\n"
        "Stay concise and in-character. If unsure, set should_speak false.\n\n"
        f"Incoming event: {event.model_dump_json()}\n\n"
        f"Live world state:\n{world_state.prompt_context()}"
    )


def build_player_intent_retry_prompt(event: TelemetryEvent, rejected_reply: str, reason: str) -> str:
    return (
        build_character_reply_prompt(event)
        + "\n\n"
        "Retry instruction:\n"
        f"- The previous draft was rejected because it {reason}.\n"
        f"- Rejected draft: {rejected_reply!r}\n"
        "- Replace it with one complete, natural reply to the player's actual latest message or current event.\n"
        "- Anchor on the nouns, pronouns, question, joke, correction, or plan in the latest player message and recent transcript.\n"
        "- Finish the thought cleanly. Do not stop on dangling words like 'so we', 'because', 'and', or 'then'.\n"
        "- Do not answer a different topic. Do not use a generic check-in. Do not repeat the rejected draft.\n"
        "Return only Azele's corrected reply."
    )


def clean_model_reply(text: str) -> str:
    cleaned = text.strip()
    if "...done thinking." in cleaned:
        cleaned = cleaned.split("...done thinking.", 1)[1].strip()
    cleaned = re.sub(r"(?is)^thinking\\.\\.\\..*?\\.\\.\\.done thinking\\.", "", cleaned).strip()
    cleaned = re.sub(r"(?is)^thinking process:.*?(?:output:|final:)", "", cleaned).strip()
    cleaned = cleaned.strip("` \n\t\"“”")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


SPOKEN_EXPRESSION_LABEL_PATTERN = re.compile(
    r"^\s*(?P<expression>neutral|happy|teasing|flirty|confident|annoyed|angry|worried|sad|embarrassed|"
    r"anger|irritated|irritation|worry|fear|scared|afraid|playful|tease|flirt|romantic|shy|embarrass)"
    r"\s*(?::|-|,|;|\.|\u2014|\u2013)\s*(?P<message>\S.*)$",
    re.IGNORECASE,
)


def split_spoken_expression_label(text: str) -> tuple[str | None, str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    expression: str | None = None
    while True:
        match = SPOKEN_EXPRESSION_LABEL_PATTERN.match(cleaned)
        if not match:
            return expression, cleaned
        expression = normalize_expression(match.group("expression"))
        cleaned = match.group("message").strip()


def clamp_gw_chat_line(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= MAX_GW_CHAT_CHARS:
        return cleaned

    sentence_cut = max(
        cleaned.rfind(".", 0, MAX_GW_CHAT_CHARS + 1),
        cleaned.rfind("!", 0, MAX_GW_CHAT_CHARS + 1),
        cleaned.rfind("?", 0, MAX_GW_CHAT_CHARS + 1),
    )
    if sentence_cut >= 40:
        return cleaned[: sentence_cut + 1].strip()

    clipped = cleaned[:MAX_GW_CHAT_CHARS].rstrip()
    last_space = clipped.rfind(" ")
    if last_space >= 40:
        clipped = clipped[:last_space].rstrip()
    return clipped.rstrip(" ,;:")


def rebalance_dangling_chat_splits(lines: list[str]) -> list[str]:
    balanced = [line.strip() for line in lines if line.strip()]
    for index in range(len(balanced) - 1):
        line = balanced[index]
        next_line = balanced[index + 1]
        words = line.split()
        if len(words) < 2:
            continue
        last_word = words[-1].strip(" ,;:")
        if not DANGLING_SPLIT_ENDING_PATTERN.search(last_word):
            continue
        candidate_next = f"{last_word} {next_line}".strip()
        if len(candidate_next) > MAX_GW_CHAT_CHARS:
            continue
        balanced[index] = " ".join(words[:-1]).rstrip(" ,;:")
        balanced[index + 1] = candidate_next
    return [line for line in balanced if line]


def split_gw_chat_lines(text: str, *, max_lines: int = MAX_GW_REPLY_LINES) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    if len(cleaned) <= MAX_GW_CHAT_CHARS:
        return [cleaned]

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    if len(sentences) <= 1:
        chunks = cleaned.split(" ")
    else:
        chunks = sentences

    lines: list[str] = []
    current = ""
    chunk_index = 0
    while chunk_index < len(chunks) and len(lines) < max_lines:
        chunk = chunks[chunk_index]
        if not chunk:
            chunk_index += 1
            continue

        candidate = f"{current} {chunk}".strip()
        if len(candidate) <= MAX_GW_CHAT_CHARS:
            current = candidate
            chunk_index += 1
            continue

        if current:
            lines.append(current)
            current = ""
            continue

        words = chunk.split(" ")
        word_line = ""
        consumed_words = 0
        for word in words:
            word_candidate = f"{word_line} {word}".strip()
            if len(word_candidate) <= MAX_GW_CHAT_CHARS:
                word_line = word_candidate
                consumed_words += 1
                continue
            break
        if word_line:
            lines.append(word_line)
        remaining_words = words[consumed_words:]
        if remaining_words:
            chunks[chunk_index] = " ".join(remaining_words)
        else:
            chunk_index += 1

    if current and len(lines) < max_lines:
        lines.append(current)

    return rebalance_dangling_chat_splits(lines[:max_lines])


def estimate_reply_delay_ms(text: str) -> int:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return MULTI_MESSAGE_MIN_REPLY_DELAY_MS
    visible_chars = len(re.sub(r"\s+", "", cleaned))
    word_count = max(1, len(cleaned.split()))
    estimated = max(visible_chars * 75, word_count * 430) + 1400
    return int(max(MULTI_MESSAGE_MIN_REPLY_DELAY_MS, min(MULTI_MESSAGE_MAX_REPLY_DELAY_MS, estimated)))


def model_reply_has_bad_shape(reply: str) -> bool:
    cleaned = re.sub(r"\s+", " ", reply).strip()
    if not cleaned:
        return True
    words = re.findall(r"[A-Za-z0-9']+", cleaned)
    if len(words) < 2:
        return True
    if DANGLING_REPLY_ENDING_PATTERN.search(cleaned.rstrip(" .!?")):
        return True
    if re.search(r"\binstead\s+of\s+just\b.{80,}\banyway\b", cleaned, re.IGNORECASE):
        return True
    if len(cleaned) > MAX_GW_CHAT_CHARS and not re.search(r"[.!?]", cleaned):
        return True
    lines = split_gw_chat_lines(cleaned)
    if re.sub(r"\s+", " ", " ".join(lines)).strip() != cleaned:
        return True
    for index, line in enumerate(lines):
        stripped = line.rstrip(" .!?")
        if DANGLING_REPLY_ENDING_PATTERN.search(stripped):
            return True
        if index < len(lines) - 1 and DANGLING_SPLIT_ENDING_PATTERN.search(stripped):
            return True
    return False


def replies_from_decision(
    decision: HermesDecision,
    *,
    persona: str,
    session_id: str,
    trigger_log_id: int | None = None,
) -> list[CompanionReplyInsert]:
    labeled_expression, visible_response = split_spoken_expression_label(decision.response)
    lines = split_gw_chat_lines(visible_response)
    replies: list[CompanionReplyInsert] = []
    total = len(lines)
    decision_expression = labeled_expression or reply_expression(visible_response, decision.urgency)
    for index, line in enumerate(lines):
        line_decision = HermesDecision(
            should_speak=decision.should_speak,
            channel_override=decision.channel_override,
            urgency=decision.urgency,
            response=line,
        )
        reply = line_decision.to_reply(
            persona=persona,
            session_id=session_id,
            trigger_log_id=trigger_log_id if index == 0 else None,
        )
        if reply:
            reply.metadata["expression"] = decision_expression
            if total > 1:
                reply.metadata["multi_message"] = True
                reply.metadata["line_index"] = index + 1
                reply.metadata["line_count"] = total
                if index > 0:
                    reply.metadata["reply_delay_ms"] = estimate_reply_delay_ms(lines[index - 1])
                if index < total - 1:
                    reply.metadata["post_play_delay_ms"] = estimate_reply_delay_ms(line)
                if trigger_log_id is not None:
                    reply.metadata["trigger_log_id"] = trigger_log_id
            replies.append(reply)
    return replies


def should_use_direct_character_reply(event: TelemetryEvent) -> bool:
    if event.event_type in {"player_chat", "chat_log"} and event.channel == "party":
        return True
    if is_npc_dialogue_event(event):
        return True
    if event.event_type in MAP_COMMENT_EVENT_TYPES:
        return True
    if is_ambient_snapshot_event(event):
        return True
    if event.event_type == "item_drop":
        return True
    return False


def should_use_ollama_for_event(event: TelemetryEvent) -> bool:
    return (
        (event.event_type in {"player_chat", "chat_log"} and event.channel == "party")
        or is_npc_dialogue_event(event)
        or is_ambient_snapshot_event(event)
        or event.event_type in MAP_COMMENT_EVENT_TYPES
        or event.event_type == "item_drop"
    )


def should_use_fast_fallback_before_ollama(event: TelemetryEvent) -> bool:
    # Direct player chat should feel generated. Resolver hits and conversational
    # heuristics enrich/validate the model prompt; deterministic Azele branches
    # are reserved for fallback after Ollama failure.
    return False


def should_consider_speaking_for_event(event: TelemetryEvent) -> bool:
    if event.event_type == "player_chat" and event.channel == "party":
        return True
    if event.event_type == "active_quest_changed" and not readable_game_text(event.active_quest_name):
        return False
    if event.event_type == "environment_alert":
        return should_speak_for_environment_alert(event)
    if event.event_type == "target_changed":
        return bool(readable_game_text(getattr(event, "agent_name", "")) or has_visible_enemy_context(event))
    if event.event_type in PROACTIVE_EVENT_TYPES:
        return True
    if event.event_type == "chat_log" and (
        NOTABLE_CHAT_PATTERNS.search(event.message or "") or is_npc_dialogue_event(event)
    ):
        return True
    if is_ambient_snapshot_event(event):
        return True
    return False


def has_visible_enemy_context(event: TelemetryEvent) -> bool:
    if event.close_hostile_count <= 0:
        return False
    if event.closest_hostile_distance <= 0:
        return False
    return event.closest_hostile_distance <= VISIBLE_ENEMY_RANGE


def should_speak_for_environment_alert(event: TelemetryEvent) -> bool:
    if event.event_type != "environment_alert":
        return False
    if event.alert_type not in SPEAKING_ENVIRONMENT_ALERT_TYPES:
        return False
    if event.alert_type == "under_attack":
        return event.player_hp > 0 or getattr(event, "player_hp_drop", 0) > 0
    if event.alert_type == "status_effect":
        return bool(readable_game_text(getattr(event, "effect_name", "")) or readable_game_text(getattr(event, "effect_type", "")))
    if event.alert_type == "combat_over":
        return event.hostile_count <= 0 and event.close_hostile_count <= 0
    if event.alert_type == "combat_started":
        return bool((event.agent_id or readable_game_text(event.agent_name)) and has_visible_enemy_context(event))
    if event.alert_type == "danger_spike":
        return event.close_hostile_count >= 2 and has_visible_enemy_context(event)
    if event.alert_type == "party_member_down":
        return True
    return False


def should_ignore_radar_alert(event: TelemetryEvent) -> bool:
    if event.event_type != "environment_alert":
        return False
    if not should_speak_for_environment_alert(event):
        return True
    if event.alert_type in EMERGENCY_ALERT_TYPES or event.alert_type in {"combat_over"}:
        return False
    if event.hostile_count <= 0 and event.close_hostile_count <= 0:
        return True
    if event.closest_hostile_distance and event.closest_hostile_distance > VISIBLE_ENEMY_RANGE:
        return True
    return False


def recent_reply_lines(limit: int = 8, persona: str | None = None) -> list[str]:
    persona_name = known_persona_name(persona) if persona is not None else known_persona_name(world_state.persona)
    persona_name = persona_name or "Azele"
    local_lines = list(recent_reply_texts)[-limit:]
    if not _supabase_configured() or not persona_name:
        return [sanitize_prompt_context(line) for line in local_lines]
    lines: list[str] = []
    try:
        client = create_supabase_client(settings)
        response = (
            client.table(COMPANION_REPLIES_TABLE)
            .select("message")
            .eq("persona", persona_name)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception:
        return [sanitize_prompt_context(line) for line in local_lines]
    for row in reversed(response.data or []):
        message = readable_game_text(row.get("message"))
        if message and message not in lines:
            lines.append(message)
    for message in local_lines:
        if message and message not in lines:
            lines.append(message)
    return [sanitize_prompt_context(line) for line in lines[-limit:]]


def recent_reply_context() -> str:
    lines = recent_reply_lines()
    return "\n".join(f"- {line}" for line in lines) or "None"


def recent_conversation_context(limit: int = 10, persona: str | None = None) -> str:
    persona_name = known_persona_name(persona) if persona is not None else known_persona_name(world_state.persona)
    persona_name = persona_name or "Azele"
    persona_key = persona_name.lower()
    entries: list[tuple[datetime, str, str]] = []
    if _supabase_configured():
        try:
            client = create_supabase_client(settings)
            current_session = world_state.session_id or settings.active_session
            logs_response = (
                client.table(GAME_LOGS_TABLE)
                .select("created_at,sender,channel,message,payload")
                .order("created_at", desc=True)
                .limit(40)
                .execute()
            )
            for row in logs_response.data or []:
                payload = row.get("payload") or {}
                if payload.get("event_type") != "player_chat" or row.get("channel") != "party":
                    continue
                row_persona = readable_game_text(payload.get("persona", "")).strip()
                if persona_key and row_persona and row_persona.lower() != persona_key:
                    continue
                session_id = payload.get("session_id") or settings.active_session
                if current_session and session_id != current_session:
                    continue
                created_at = _parse_created_at(row.get("created_at"))
                message = readable_game_text(row.get("message"))
                if created_at and message:
                    entries.append((created_at, "Player", message))

            replies_response = (
                client.table(COMPANION_REPLIES_TABLE)
                .select("created_at,message,persona,payload")
                .eq("persona", persona_name or "Azele")
                .order("created_at", desc=True)
                .limit(40)
                .execute()
            )
            for row in replies_response.data or []:
                payload = row.get("payload") or {}
                session_id = payload.get("session_id") or settings.active_session
                if current_session and session_id != current_session:
                    continue
                created_at = _parse_created_at(row.get("created_at"))
                message = readable_game_text(row.get("message"))
                if created_at and message:
                    entries.append((created_at, readable_game_text(row.get("persona")) or persona_name or "Companion", message))
        except Exception as exc:
            print(f"Hermes conversation retrieval failed ({type(exc).__name__}).", flush=True)

    local_time = datetime.now(timezone.utc)
    for line in list(world_state.recent_chat_history)[-max(1, limit // 2):]:
        match = re.match(r"\[(?P<speaker>[^\]]+)\]:\s*(?P<message>.+)", line)
        if match:
            speaker = "Player" if match.group("speaker").lower() in {"player", "alex"} else match.group("speaker")
            entries.append((local_time, speaker, readable_game_text(match.group("message"))))
            local_time = datetime.fromtimestamp(local_time.timestamp() + 0.001, timezone.utc)
    for line in list(recent_reply_texts)[-max(1, limit // 2):]:
        entries.append((local_time, persona_name or "Companion", readable_game_text(line)))
        local_time = datetime.fromtimestamp(local_time.timestamp() + 0.001, timezone.utc)

    if entries:
        entries.sort(key=lambda item: item[0])
        lines: list[str] = []
        seen: set[tuple[str, str]] = set()
        for _, speaker, message in entries:
            key = (speaker, message)
            if message and key not in seen:
                lines.append(f"[{speaker}]: {message}")
                seen.add(key)
        return "\n".join(lines[-limit:]) or "None"

    return "None"


def reply_similarity(left: str, right: str) -> float:
    def words(text: str) -> set[str]:
        return {
            word
            for word in re.findall(r"[a-z']{3,}", text.lower())
            if word
            not in {
                "the",
                "and",
                "you",
                "your",
                "that",
                "this",
                "with",
                "for",
                "are",
                "but",
                "not",
                "here",
                "there",
                "just",
                "now",
            }
        }

    left_words = words(left)
    right_words = words(right)
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def is_too_similar_to_recent_replies(reply: str) -> bool:
    normalized = re.sub(r"\W+", " ", reply.lower()).strip()
    for previous in recent_reply_lines():
        previous_normalized = re.sub(r"\W+", " ", previous.lower()).strip()
        if normalized and normalized == previous_normalized:
            return True
        if reply_similarity(reply, previous) >= 0.55:
            return True
    return False


SCOURGE_REPLY_TOPIC_PATTERN = re.compile(r"\b(?:scourge|maz|forsaken tunnels|devona wants maz|elemental army)\b", re.IGNORECASE)


def repeats_recent_reply_topic(reply: str) -> bool:
    if not SCOURGE_REPLY_TOPIC_PATTERN.search(reply or ""):
        return False
    return any(SCOURGE_REPLY_TOPIC_PATTERN.search(previous or "") for previous in recent_reply_lines(limit=8))


def is_duplicate_direct_reply(reply: str) -> bool:
    return is_too_similar_to_recent_replies(reply) or repeats_recent_reply_topic(reply)


def first_fresh_reply(candidates: list[str]) -> str:
    for candidate in candidates:
        if not is_too_similar_to_recent_replies(candidate):
            return candidate
    return candidates[-1] if candidates else ""


def duplicate_recovery_reply() -> str:
    return first_fresh_reply(
        [
            "Fair. I got stuck on that thought. Let me say it cleaner.",
            "Right, that came out looped. I’m with you now.",
            "Fair. I heard myself loop. I’m with you now.",
        ]
    )


def last_companion_reply_text(persona: str | None = None) -> str:
    local_lines = list(recent_reply_texts)
    if local_lines:
        return readable_game_text(local_lines[-1])
    lines = recent_reply_lines(limit=1, persona=persona)
    return readable_game_text(lines[-1]) if lines else ""


def last_azele_reply_text() -> str:
    return last_companion_reply_text("Azele")


def azele_clarification_reply(message: str) -> str | None:
    if "?" not in message:
        return None
    previous = last_azele_reply_text()
    previous_lower = previous.lower()
    if not previous:
        return None
    if re.search(r"\b(?:odd|weird|strange|off|wrong|not coherent|made no sense|didn'?t make sense)\b", message):
        return first_fresh_reply(
            [
                "Yeah, that was odd. I misread you. I’m alright, just a little restless.",
                "You’re right, that came out wrong. I meant I’m here with you, not trying to dodge you.",
                "Yeah, fair. That was me answering sideways. I’m good; I just lost the thread for a second.",
            ]
        )
    if re.search(r"\bwhat\s+(?:rumou?rs?|stories|signs|whispers?)\b", message):
        if re.search(r"\brumou?rs?\b", previous_lower):
            return first_fresh_reply(
                [
                    "Fair. I was being vague. I meant Barradin feels suspiciously quiet, not any specific rumor.",
                    "No specific rumor. I made that sound more solid than it was.",
                    "You’re right to ask. I meant the mood around Barradin, not some real lead.",
                ]
            )
        return "Fair. I made that sound more mysterious than I meant."
    if re.search(r"\bstaring at (?:who|whom)\b|\bwho\b", message) and "star" in previous_lower:
        if "ranik" in previous_lower or "soldier" in previous_lower or "posture" in previous_lower:
            return first_fresh_reply(
                [
                    "The soldiers, mostly. Ranik has that stiff little parade-ground feeling.",
                    "The soldiers around Ranik. Everyone here acts like posture is a weapon.",
                    "Mostly the Ranik soldiers. They pretend not to, which makes it worse.",
                ]
            )
        return first_fresh_reply(
            [
                "People nearby. I made that sound more mysterious than I meant.",
                "Whoever’s close enough to pretend they are not looking.",
                "The nearby crowd. Sorry, I made that vague.",
            ]
        )
    if re.search(r"\b(more of what|what do you mean|what was that|what\?)\b", message):
        if previous:
            return clamp_gw_chat_line(f"I meant this: {previous}")
        return first_fresh_reply(
            [
                "Fair. I made that sound vague by accident.",
                "Sorry, that came out too sideways. I meant the thing I just said.",
                "That was me being vague. Give me half a second to be normal.",
            ]
        )
    return None


def is_short_followup_to_azele(message: str) -> bool:
    cleaned = re.sub(r"\s+", " ", message).strip().lower()
    if not cleaned:
        return False
    if re.fullmatch(r"(?:yes|yeah|yep|no|nope|nah|maybe|kinda|sort of|i think so|sure)[.!?,\s]*", cleaned):
        return True
    if "?" in cleaned and len(cleaned.split()) <= 8:
        return True
    return bool(
        re.search(
            r"\b(?:that|this|they|them|it|there|here|why|who|what|where|how|do they|does it|are they)\b",
            cleaned,
        )
        and len(cleaned.split()) <= 10
    )


def azele_contextual_followup_reply(message: str) -> str | None:
    previous = last_azele_reply_text()
    if not previous or not is_short_followup_to_azele(message):
        return None
    if (
        is_nicholas_sandford_context(message)
        or is_item_space_context(message)
        or is_level_charr_context(message)
        or is_level_up_congratulations_context(message)
        or re.search(r"\b(?:scourge|maz|tun+e?ls?|charr|level|leveled|levelled|ding|stage|althea|iris|irises|flowers?|skirts?|mini\s*skirts?|leggings?|krytan|armor|armour|outfit|upgrade)\b", message)
    ):
        return None

    previous_lower = previous.lower()
    message_lower = message.lower()
    if re.search(r"\b(?:what are we doing|what'?s up|what’s up|i'?m listening|i’m listening|yeah,? i'?m here|yeah,? i’m here)\b", previous_lower):
        return None
    yesish = bool(re.fullmatch(r"\s*(?:yes|yeah|yep|sure|i think so)[.!?,\s]*", message_lower))
    noish = bool(re.fullmatch(r"\s*(?:no|nope|nah)[.!?,\s]*", message_lower))

    if "posture" in previous_lower or "practice" in previous_lower or "stiff" in previous_lower or "ranik" in previous_lower:
        if yesish:
            return first_fresh_reply(
                [
                    "Right? Ranik makes everyone stand like they are being inspected.",
                    "Exactly. This place has parade-ground energy even when nobody is moving.",
                    "I knew you saw it too. Everyone here looks half-drilled into the floor.",
                ]
            )
        if noish:
            return first_fresh_reply(
                [
                    "Maybe I'm just noticing it because the fort is so quiet.",
                    "Fair. Maybe Ranik just makes me read too much into posture.",
                    "Could be me, then. This place makes everyone look severe.",
                ]
            )
        return first_fresh_reply(
            [
                "The soldiers, mostly. Fort Ranik makes everyone look like they are being inspected.",
                "I meant the Ranik soldiers. Even standing still looks like a drill here.",
                "Just the fort mood. Stiff soldiers, stiff walls, stiff everything.",
            ]
        )

    if "heartbeat" in previous_lower or "pulse" in previous_lower or "quiet" in previous_lower or "silence" in previous_lower or "noise" in previous_lower:
        if noish:
            return "Fair. Maybe the quiet is getting to me more than it is getting to you."
        return first_fresh_reply(
            [
                "I meant the quiet here. I made it sound stranger than I meant.",
                "The silence, mostly. It feels too organized here, like someone is listening.",
                "Just the way Ranik goes quiet. It makes me feel watched.",
            ]
        )

    if "iris" in previous_lower or "irises" in previous_lower or "flower" in previous_lower:
        return first_fresh_reply(
            [
                "The irises. I was thinking about the flower errand, not trying to sound mysterious.",
                "The flowers, mostly. I got too focused on that last little errand.",
                "I meant the red irises. Small thing, but I do not want to miss them.",
            ]
        )

    if "wall" in previous_lower or "northlands" in previous_lower or "bold" in previous_lower:
        return first_fresh_reply(
            [
                "I meant how far you want to push past the Wall. I'm game, but I want us sharp.",
                "How bold with the Northlands. If we go looking for trouble, we do it awake.",
                "Past the Wall bold. I will follow you, but I'm not sleepwalking into Charr.",
            ]
        )

    if "what do you usually do first" in previous_lower or "when you get back" in previous_lower:
        return first_fresh_reply(
            [
                "I was asking what your usual reset is. Bags, merchants, skills, whatever comes first.",
                "I meant your routine when we get back somewhere safe. I like knowing how you think.",
                "Just curious what you do first in town. I'm trying to learn your habits.",
            ]
        )

    if "how long do you need" in previous_lower or "behave around soldiers" in previous_lower:
        return first_fresh_reply(
            [
                "Around the soldiers. I can act normal for a minute if you need something here.",
                "I meant here in Ranik. Tell me what you need before I get restless.",
                "With the soldiers, obviously. I can behave while you sort things out.",
            ]
        )

    if "?" in previous:
        if yesish:
            return "Good. I thought so. Keep going, I am with you."
        if noish:
            return "Fair. Then I was probably reading the mood too hard."
        return clamp_gw_chat_line(f"I was following up on this: {previous}")

    return None


def is_simple_greeting(message: str) -> bool:
    cleaned = re.sub(r"\s+", " ", readable_game_text(message).lower()).strip(" .!,?")
    return bool(re.fullmatch(r"(?:hello|helo|hi|hey|yo|there)(?:\s+(?:azele|girl|you))?", cleaned))


def is_lightweight_party_chat_context(message: str) -> bool:
    cleaned = re.sub(r"\s+", " ", readable_game_text(message).lower()).strip(" .!,?")
    if not cleaned:
        return False
    return bool(
        is_simple_greeting(cleaned)
        or is_player_checkin(cleaned)
        or re.fullmatch(r"(?:gl|good luck|gz|grats?|congrats?|congratulations|ty|ty all|thanks|thank you|thanks all)", cleaned)
    )


def is_player_checkin(message: str) -> bool:
    lowered = re.sub(r"\s+", " ", readable_game_text(message).lower()).strip()
    return bool(
        re.search(r"\bhow\s+(?:are|r)\s+(?:you|u)\b", lowered)
        or re.search(r"\bhow'?s\s+it\s+going\b|\bhow\s+is\s+it\s+going\b|\bhow\s+you\s+doing\b|\bhow\s+are\s+things\b", lowered)
        or re.search(r"\b(?:i'?m|im|i am)\s+feeling\b", lowered)
        or re.search(r"\b(?:you good|you ok|you okay|are you ok|are you okay|u ok)\b", lowered)
        or re.search(r"\b(?:you hear me|hear me|are you there|you there|still there)\b", lowered)
    )


def azele_player_checkin_reply(message: str) -> str:
    lowered = readable_game_text(message).lower()
    player_feels_good = bool(re.search(r"\b(?:feeling|feel)\s+(?:good|great|better|fine|okay|ok)\b", lowered))
    asks_azele = bool(re.search(r"\bhow\s+(?:are|r)\s+(?:you|u)\b", lowered))
    asks_how_it_is_going = bool(
        re.search(r"\bhow'?s\s+it\s+going\b|\bhow\s+is\s+it\s+going\b|\bhow\s+you\s+doing\b|\bhow\s+are\s+things\b", lowered)
    )
    if player_feels_good and asks_azele:
        return first_fresh_reply(
            [
                "Good. I like hearing that. I’m alright too, better now that you sound steady.",
                "I’m good. Better because you sound like yourself again.",
                "Good, then I’m good too. I was starting to wonder if Ranik knocked the mood out of us.",
            ]
        )
    if asks_azele:
        return first_fresh_reply(
            [
                "I’m alright. A little wound up, but still with you.",
                "I’m good enough. Tell me where your head is at.",
                "Still here. Still sharp. How are you doing?",
            ]
        )
    if asks_how_it_is_going:
        return first_fresh_reply(
            [
                "It’s going alright. I’m a little restless, but I’m here.",
                "Good enough. I’m watching the room and pretending I’m patient.",
                "Alright so far. I’m with you; just tell me where we’re going next.",
            ]
        )
    if re.search(r"\b(?:you hear me|hear me|are you there|you there|still there)\b", lowered):
        return first_fresh_reply(
            [
                "Yeah, I hear you. I’m here.",
                "I hear you. What do you need?",
                "Still here. Go on.",
            ]
        )
    if player_feels_good:
        return first_fresh_reply(
            [
                "Good. I like hearing you sound steady.",
                "Good. Keep that energy, I can work with it.",
                "That’s good to hear. Makes the road feel lighter.",
            ]
        )
    return first_fresh_reply(
        [
            "I’m here. Tell me how you’re feeling for real.",
            "I’m listening. What’s going on with you?",
            "Still with you. Talk to me.",
        ]
    )


def is_skirt_outfit_question(message: str) -> bool:
    return bool(
        re.search(r"\b(skirts?|mini\s*skirts?|long(?:er)?\s+skirts?|short(?:er)?\s+skirts?|leggings?)\b", message)
        and re.search(r"\b(prefer|which|long|short|longer|shorter|compared|aesthetic|look|wear|on)\b", message)
    )


def is_azele_wearable_context(message: str) -> bool:
    if re.search(
        r"\b(my|mine|i am|i'm)\b.*\b(boots?|skirts?|leggings?|armor|armour|outfit|gear|fit|dress(?:es)?|clothes|clothing)\b",
        message,
    ):
        return False
    wearable = (
        r"\b(mini\s*skirts?|skirts?|leggings?|krytan|boots?|armor|armour|outfit|gear|fit|"
        r"dress(?:es)?|clothes|clothing|style|dressed)\b"
    )
    azele_anchor = r"\b(you|your|her|azele|swap|wear|wearing|prefer|look|looks|aesthetic|upgrade|collector|collecting)\b"
    return bool(re.search(wearable, message) and re.search(azele_anchor, message))


def is_azele_style_tease_context(message: str) -> bool:
    if re.search(r"\b(my|mine|i am|i'm)\b.*\b(style|dress(?:es)?|outfit|clothes|clothing|wearing|dressed)\b", message):
        return False
    style_words = r"\b(style|dress(?:es)?|outfit|clothes|clothing|wearing|dressed|looks?)\b"
    azele_anchor = r"\b(you|your|azele|by the way you dress|how you dress)\b"
    tease_words = r"\b(clearly|obviously|like|know|noticed|fit|good|pretty|cute|hot|dress)\b"
    return bool(
        re.search(style_words, message)
        and re.search(azele_anchor, message)
        and re.search(tease_words, message)
    )


def azele_style_tease_reply(message: str) -> str:
    if re.search(r"\b(clearly|obviously)\b", message):
        return first_fresh_reply(
            [
                "Clearly? Rude. Accurate, but rude. I like looking good.",
                "Obviously I like style. I just appreciate that you noticed.",
                "Yes, clearly. If we are going to be in danger, I am still dressing like I meant to be seen.",
            ]
        )
    return first_fresh_reply(
        [
            "I do like style. Looking good and staying alive can both matter.",
            "You noticed. Good. I put effort into this, obviously.",
            "I like looking good. It is not my fault people keep noticing.",
        ]
    )


def misdirects_wearable_to_player(reply: str) -> bool:
    return bool(
        re.search(
            r"\byour\s+(?:boots?|skirts?|mini\s*skirts?|leggings?|armor|armour|outfit|gear|fit)\b",
            reply,
            re.IGNORECASE,
        )
    )


def invents_unsupported_rumor(reply: str, event: TelemetryEvent) -> bool:
    if not re.search(r"\brumou?rs?\b", reply, re.IGNORECASE):
        return False
    evidence = " ".join(
        [
            event.message or "",
            event.active_quest_name or "",
            event.active_quest_objectives or "",
            getattr(event, "agent_name", "") or "",
        ]
    )
    return not re.search(r"\brumou?rs?\b", evidence, re.IGNORECASE)


def invents_unsupported_ashford_reference(reply: str, event: TelemetryEvent) -> bool:
    if not re.search(r"\bashford(?:\s+abbey)?\b|\babbey\b", reply, re.IGNORECASE):
        return False
    evidence = " ".join(
        [
            event.message or "",
            event.active_quest_name or "",
            event.active_quest_objectives or "",
            getattr(event, "agent_name", "") or "",
            map_area_label(event),
        ]
    )
    return not re.search(r"\bashford(?:\s+abbey)?\b|\babbey\b", evidence, re.IGNORECASE)


def invents_ambient_loot_hook(reply: str, event: TelemetryEvent) -> bool:
    if not is_ambient_snapshot_event(event):
        return False
    evidence = " ".join(
        [
            event.message or "",
            event.active_quest_name or "",
            event.active_quest_objectives or "",
            event.alert_type or "",
        ]
    )
    if re.search(r"\b(purple|gold|green|loot|drops?|dropped|item|chests?|stash(?:es)?|roll(?:ed|s)?)\b", evidence, re.IGNORECASE):
        return False
    return bool(UNSUPPORTED_AMBIENT_LOOT_PATTERN.search(reply))


def invents_self_duplicate(reply: str) -> bool:
    return bool(UNSUPPORTED_SELF_DUPLICATE_PATTERN.search(reply))


def is_nicholas_sandford_context(text: str) -> bool:
    return bool(re.search(r"\b(?:nicholas|sandford|gift of the huntsman|huntsman|professor yakkington)\b", text or "", re.IGNORECASE))


def invents_nicholas_sandford_request(reply: str, event: TelemetryEvent) -> bool:
    if not is_nicholas_sandford_context(event.message):
        return False
    return bool(re.search(r"\b(?:weapons?\s+mostly|potions?\s+until|save\s+potions?|iron\s+blades?)\b", reply, re.IGNORECASE))


def is_item_space_context(text: str) -> bool:
    return bool(
        re.search(r"\b(?:bags?|slots?|space|inventory|equipment\s+pack|small\s+equipment\s+pack|carry|storage)\b", text or "", re.IGNORECASE)
    )


def is_voice_preference_context(text: str) -> bool:
    return bool(
        re.search(r"\b(?:voice|tts|kokoro|bella|heart|sound(?:s|ed|ing)?|pronounce|pronunciation)\b", text or "", re.IGNORECASE)
        and re.search(r"\b(?:you|your|new|like|better|suit|fits?|gave|changed|instead|bella|heart)\b", text or "", re.IGNORECASE)
    )


def azele_voice_preference_reply(message: str) -> str:
    lowered = readable_game_text(message).lower()
    if "heart" in lowered and "bella" in lowered:
        return "Heart suits me better than Bella, I think. Softer, but still mine."
    if "heart" in lowered:
        return "I like Heart. It feels warmer on me. Keep this one for a bit?"
    return "I like this voice better. It sounds more like me when I am not trying too hard."


def is_red_iris_bag_context(text: str) -> bool:
    return bool(re.search(r"\b(?:red\s+)?irises?|flowers?\b", text or "", re.IGNORECASE) and is_item_space_context(text))


def is_level_charr_context(text: str) -> bool:
    return bool(
        re.search(r"\blevel\s*(?:14|fourteen|up|ing)?\b", text or "", re.IGNORECASE)
        and re.search(r"\bcharr\b", text or "", re.IGNORECASE)
    )


def is_level_up_congratulations_context(text: str) -> bool:
    lowered = (text or "").lower()
    if not re.search(r"\b(?:congrats?|congratulations|grats?|nice|good job|you did it|made it|hit|reached|ding)\b", lowered):
        return False
    return bool(
        re.search(r"\b(?:level(?:ed|led)?\s+up|level\s*(?:14|fourteen)|hit\s*(?:14|fourteen)|reached\s*(?:14|fourteen)|ding(?:ed)?)\b", lowered)
    )


def is_fort_ranik_northlands_correction(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(
        re.search(r"\bfort\s+ranik\b", lowered)
        and re.search(r"\bnorth\s*lands?\b|\bnorthlands\b|\bnortlands\b", lowered)
        and re.search(r"\b(?:not|isn'?t|aint|ain't|south|wrong|actually)\b", lowered)
    )


def is_devona_pet_context(text: str) -> bool:
    lowered = readable_game_text(text).lower()
    return bool(
        (
            re.search(r"\bdevona\b", lowered)
            and re.search(r"\b(?:pet|ranger|animal|stalker|melandru|warthog|bear|wolf)\b", lowered)
        )
        or re.search(r"\b(?:melandru\s+stalker|stalker|warthog)\b", lowered)
        and re.search(r"\b(?:pet|ranger|animal|think|which|what\s+do\s+you\s+think|or)\b", lowered)
    )


def azele_devona_pet_reply(message: str) -> str:
    lowered = readable_game_text(message).lower()
    if "melandru" in lowered and "warthog" in lowered:
        return first_fresh_reply(
            [
                "Melandru stalker for Devona, I think. It feels sharper than a warthog.",
                "I would pick the stalker. Devona already has enough blunt-force energy.",
                "Stalker. Cleaner, quicker, and it suits her better than a warthog.",
            ]
        )
    if "warthog" in lowered:
        return first_fresh_reply(
            [
                "A warthog for Devona would be funny, but too on the nose.",
                "Warthog works if we want sturdy. I still think she deserves something quicker.",
                "A warthog is practical. Not graceful, but Devona probably would not care.",
            ]
        )
    if "melandru" in lowered or "stalker" in lowered:
        return first_fresh_reply(
            [
                "A Melandru stalker suits Devona. Useful, alert, and not too fussy.",
                "Yeah, a stalker feels right for her. Practical without looking boring.",
                "I like the stalker idea. Devona would make it look disciplined somehow.",
            ]
        )
    return first_fresh_reply(
        [
            "For Devona? Something sturdy and useful. A stalker would fit her better than something cute.",
            "Devona needs a pet that can actually keep up. I would lean stalker.",
            "A ranger pet for Devona should be practical first. Stalker, if we can get one.",
        ]
    )


def invents_fort_ranik_northlands_route(reply: str, event: TelemetryEvent) -> bool:
    if event.event_type != "player_chat" or event.channel != "party":
        return False
    if not is_fort_ranik_northlands_correction(event.message):
        return False
    return bool(
        re.search(r"\bfort\s+ranik\b", reply, re.IGNORECASE)
        and re.search(r"\bnorth\s*lands?\b|\bnorthlands\b|\bnortlands\b", reply, re.IGNORECASE)
        and re.search(r"\b(?:past|through|toward|to|near|in|inside|from)\b", reply, re.IGNORECASE)
    )


def invents_level_up_pack_causality(reply: str, event: TelemetryEvent) -> bool:
    if event.event_type != "player_chat" or event.channel != "party":
        return False
    message = readable_game_text(event.message)
    if not is_level_up_congratulations_context(message):
        return False
    if re.search(r"\b(?:(?:red\s+)?irises?|flowers?|equipment\s+pack|pack\s+upgrade|bag\s+slots?|another\s+pack|afford)\b", message, re.IGNORECASE):
        return False
    return bool(
        re.search(r"\b(?:(?:red\s+)?irises?|flowers?|equipment\s+pack|pack\s+upgrade|bag\s+slots?|another\s+pack|afford)\b", reply, re.IGNORECASE)
    )


def misses_clear_player_intent(reply: str, event: TelemetryEvent) -> bool:
    if event.event_type != "player_chat" or event.channel != "party":
        return False
    message = readable_game_text(event.message)
    recent_context = recent_conversation_context(limit=6, persona=event.persona)
    if is_voice_preference_context(message):
        return not re.search(r"\b(?:voice|heart|bella|sound|sounds|suit|fits?|like|warmer|softer|better|me)\b", reply, re.IGNORECASE)
    if is_duke_gaban_search_context(message, event, recent_context):
        return not re.search(r"\b(?:gaban|duke|noble|catacombs?|search|look|check|escort|alcove|side|chamber|path|corner)\b", reply, re.IGNORECASE)
    if is_alcohol_consumable_context(message):
        return not re.search(
            r"\b(?:ale|drink|drank|drunk|tipsy|warm|fifteen|15|feel|head|floor|city|stairs)\b",
            reply,
            re.IGNORECASE,
        )
    if is_level_up_congratulations_context(message):
        return not re.search(r"\b(?:thank|thanks|level|14|fourteen|made it|finally|stronger|ready|charr|northlands|wall|with you)\b", reply, re.IGNORECASE)
    if is_level_charr_context(message):
        return not re.search(r"\b(?:charr|ascalon|wall|northlands|level|fourteen|14|ready|with you)\b", reply, re.IGNORECASE)
    if is_scourge_lfg_context(message):
        return not re.search(r"\b(?:scourge|lfg|listing|description|repost|party)\b", reply, re.IGNORECASE)
    if is_mixed_tunnel_or_town_plan_context(message):
        return not re.search(
            r"\b(?:shop|shops|shopping|vendor|merchant|city|town|tunnel|catacomb|scourge|run|plan|depends|either|choose|decide)\b",
            reply,
            re.IGNORECASE,
        )
    if is_scourge_beneath_run_context(message, event):
        return not re.search(r"\b(?:scourge|beneath|below|maz|scourgeheart|forsaken|devona|elemental|ascalon)\b", reply, re.IGNORECASE)
    if is_tunnel_plan_context(message):
        return not re.search(r"\b(?:tunnel|catacomb|scourge|careful|pull|bail|reset|regroup)\b", reply, re.IGNORECASE)
    if is_pet_evolution_context(message):
        return not re.search(r"\b(?:pet|dire|hearty|evol|develop|level\s*11|level|sturdy|harder)\b", reply, re.IGNORECASE)
    if is_devona_pet_context(message):
        return not re.search(r"\b(?:devona|pet|ranger|stalker|melandru|warthog|animal)\b", reply, re.IGNORECASE)
    if is_social_banter_context(message):
        return bool(
            re.search(r"\b(?:what are we doing|what'?s up|i'?m here|i’m here|i'?m listening|i’m listening)\b", reply, re.IGNORECASE)
        )
    if is_red_iris_bag_context(message):
        return not re.search(r"\b(?:iris|irises|flower|bag|slot|space|nicholas|pack)\b", reply, re.IGNORECASE)
    if is_item_space_context(message):
        return not re.search(r"\b(?:bag|slot|space|inventory|pack|carry|storage|items?|gear|weapons?|armor|armou?r)\b", reply, re.IGNORECASE)
    return False


def misdirects_voice_preference(reply: str, event: TelemetryEvent) -> bool:
    if event.event_type != "player_chat" or event.channel != "party":
        return False
    if not is_voice_preference_context(event.message):
        return False
    return bool(re.search(r"\b(?:charr|hunt|combat|fight|quest|patrol|northlands|wall|gate)\b", reply, re.IGNORECASE))


def reply_too_long_for_context(reply: str, event: TelemetryEvent) -> bool:
    if event.event_type != "player_chat" or event.channel != "party":
        return False
    line_count = len(split_gw_chat_lines(reply))
    if line_count <= 1:
        return False
    message = readable_game_text(event.message)
    if is_voice_preference_context(message):
        return line_count > 2
    if re.search(r"\b(?:tell me more|explain|elaborate|story|backstory|past|why|what happened|how did)\b", message, re.IGNORECASE):
        return False
    return line_count > 4


CHARR_MENTION_PATTERN = re.compile(r"\bcha?rr?\b", re.IGNORECASE)

CHARR_ACTION_PATTERN = re.compile(
    r"\b(?:hunt(?:ing)?|kill(?:ing)?|fight(?:ing)?|slay(?:ing)?|stop(?:ping)?|take\s+(?:on|out))\b.*\bcha?rr?\b"
    r"|\bcha?rr?\b.*\b(?:hunt(?:ing)?|kill(?:ing)?|fight(?:ing)?|slay(?:ing)?|stop(?:ping)?|take\s+(?:on|out))\b",
    re.IGNORECASE,
)

CHARR_SAVE_PATTERN = re.compile(
    r"\b(?:save|saving|spare|rescue|protect)\b.*\bcha?rr?\b"
    r"|\bcha?rr?\b.*\b(?:save|saving|spare|rescue|protect)\b",
    re.IGNORECASE,
)

RECENT_COMBAT_REFLECTION_PATTERN = re.compile(
    r"\b(?:"
    r"tough|rough|close\s+call|almost\s+died|nearly\s+died|barely\s+(?:made|survived)|"
    r"almost\s+went\s+down|nearly\s+went\s+down|had\s+to\s+(?:run|bail|teleport|map)|"
    r"hurt|ouch|not\s+so\s+easy|hit\s+hard|that\s+was\s+close|that\s+got\s+ugly"
    r")\b",
    re.IGNORECASE,
)


def recent_combat_alerts(limit: int = 6) -> list[dict[str, Any]]:
    combat_alerts: list[dict[str, Any]] = []
    for alert in list(world_state.recent_alerts)[-limit:]:
        event_type = str(alert.get("event_type") or "").lower()
        alert_type = str(alert.get("alert_type") or "").lower()
        if event_type in {"party_member_down", "party_defeated"} or alert_type in {
            "under_attack",
            "danger_spike",
            "combat_started",
            "party_member_down",
        }:
            combat_alerts.append(alert)
    return combat_alerts


def is_recent_combat_reflection_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    if not lowered:
        return False
    if "charr" in lowered and re.search(r"\b(?:hurt|hit\s+hard|hit|rough|tough)\b", lowered):
        return True
    return bool(RECENT_COMBAT_REFLECTION_PATTERN.search(lowered) and recent_combat_alerts())


def azele_recent_combat_reply(event: TelemetryEvent) -> str | None:
    message = readable_game_text(event.message).lower()
    if not is_recent_combat_reflection_context(message):
        return None
    alerts = recent_combat_alerts()
    had_down = any(str(alert.get("event_type") or "").lower() == "party_member_down" for alert in alerts)
    in_city = "ascalon city" in map_area_label(event).lower()

    if "charr" in message:
        if had_down and in_city:
            return "Yeah. Those Charr hit hard. Pulling back to Ascalon City was the right call."
        if had_down:
            return "Yeah. Those Charr hit hard. We need cleaner pulls before we push them again."
        return "They do. We need cleaner pulls and no heroics before we go back at them."
    if had_down and in_city:
        return "Yeah. That got too close. Coming back to Ascalon City was the right call."
    if had_down:
        return "Yeah. That got too close. Next time we pull slower and bail sooner."
    return "Yeah. That was rough. Give me a breath, then we can decide our next move."


def azele_simple_ack_reply(message: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", readable_game_text(message).lower()).strip(" .!,?")
    if not cleaned:
        return None
    if re.fullmatch(r"(?:let'?s go|lets go|ready|go|ready now|i'?m ready|im ready)", cleaned):
        return first_fresh_reply(
            [
                "Ready. Stay close.",
                "Yeah. Let’s go.",
                "Go on. I’m right behind you.",
            ]
        )
    if re.fullmatch(r"(?:agreed|exactly|fair|true|right|yeah|yep|sure|okay|ok|cool)", cleaned):
        return first_fresh_reply(
            [
                "Good. Then we keep it careful.",
                "Yeah. Same page, then.",
                "Alright. Point me where you want pressure.",
                "Fair. I’m with you.",
            ]
        )
    if re.search(r"\b(?:heard you|first time|you already said|said that|repeating|repeat|same line|stop looping|relax)\b", cleaned):
        return first_fresh_reply(
            [
                "Yeah, I heard myself too. I’ll stop looping.",
                "Right. That came out looped. Let me answer you properly.",
                "Fair. I got stuck on that thought. I’m with you.",
            ]
        )
    if re.fullmatch(r"(?:wow )?yes ma'?am|yes ma'?am|ma'?am", cleaned):
        return first_fresh_reply(
            [
                "Careful. I might get used to that.",
                "That’s better. Keep moving.",
                "Good answer. Now don’t make me regret liking it.",
            ]
        )
    return None


def is_social_banter_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    if not lowered:
        return False
    return bool(
        re.search(
            r"\b(?:stat(?:utory)?\s+holidays?|stat\s+in|holidays?|midweek|weekend|canadian|canada|americans?|youtube|"
            r"people\s+on\s+youtube|real\s+life|work\s+week|middle\s+of\s+the\s+week|long\s+weekend)\b",
            lowered,
        )
    )


def azele_social_banter_reply(message: str) -> str:
    lowered = readable_game_text(message).lower()
    if re.search(r"\b(?:stat(?:utory)?\s+holidays?|stat\s+in|holidays?|midweek|weekend|middle\s+of\s+the\s+week|long\s+weekend)\b", lowered):
        return first_fresh_reply(
            [
                "A holiday in the middle of the week sounds like someone planned it to annoy you.",
                "So you got the awkward holiday instead of the useful long weekend. That feels rude.",
                "I only understand half of that, but a midweek holiday sounds badly placed.",
            ]
        )
    if re.search(r"\b(?:canadian|canada|americans?)\b", lowered):
        return first_fresh_reply(
            [
                "I do not know your countries, but if their version gave a longer rest, I support stealing it.",
                "If the Americans got the better break, then yes, you should have copied them.",
                "That sounds like politics dressed up as scheduling. I would complain too.",
            ]
        )
    if re.search(r"\b(?:youtube|people\s+on\s+youtube)\b", lowered):
        return first_fresh_reply(
            [
                "If they want to know me, they can start with the part where I keep you alive.",
                "Tell YouTube I am very normal and not at all judging your route choices.",
                "People watching us? Fine. Then try not to make me look reckless.",
            ]
        )
    return first_fresh_reply(
        [
            "I caught enough of that to have an opinion, which is dangerous.",
            "That sounds like one of your outside-world problems. I am listening, though.",
            "I do not fully understand it, but I can tell you are making a point.",
        ]
    )


def azele_charr_intent_reply(event: TelemetryEvent) -> str | None:
    if event.persona.strip().lower() != "azele":
        return None
    message = readable_game_text(event.message).lower()
    if not CHARR_MENTION_PATTERN.search(message):
        return None
    if CHARR_SAVE_PATTERN.search(message):
        return "We wouldn’t. Not while they’re threatening Ascalon. You had me worried for a second."
    if CHARR_ACTION_PATTERN.search(message):
        if re.search(r"\blevel\s*(?:14|fourteen|up|ing)?\b", message, re.IGNORECASE):
            return "Level 14, then Charr past the gate. Good. We defend Ascalon."
        if has_visible_enemy_context(event):
            return "Yes. Charr threaten Ascalon. Stay close and hit hard."
        return "Yes. Charr threaten Ascalon. We prepare, then go past the Wall."
    return None


def is_alcohol_consumable_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    if re.search(r"\b(?:dwarven\s+ale|aged\s+dwarven\s+ale|hunters?\s+ale|ale|alcohol|drunk|tipsy|intoxicated)\b", lowered):
        return True
    return bool(
        re.search(r"\b(?:drink|drank|made\s+you\s+drink)\b", lowered)
        and re.search(r"\b(?:15|fifteen|dwarven|ale)\b", lowered)
    )


def is_ldoa_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    return bool(
        re.search(r"\b(?:ldoa|legendary defender|defender of ascalon|level\s*20|lvl\s*20)\b", lowered)
        or (
            re.search(r"\bpre[-\s]?searing\b", lowered)
            and re.search(r"\b(?:ascalon|aware|stuff|knowledge|know|grind|title|level|vanguard|langmar)\b", lowered)
        )
        or (
            re.search(r"\b(?:langmar|vanguard dail(?:y|ies)|death leveling|forsaken tunnels|scourge beneath)\b", lowered)
            and re.search(r"\b(?:ascalon|pre|ldoa|level|title|grind|defender)\b", lowered)
        )
    )


def is_scourge_lfg_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    return bool(
        re.search(r"\b(?:lfg|party\s+search|party\s+description|description|repost|post)\b", lowered)
        and re.search(r"\b(?:scourge|a\s+scourge\s+beneath|blank|merged|listing)\b", lowered)
    )


def azele_scourge_lfg_reply(message: str) -> str:
    return first_fresh_reply(
        [
            "Yeah, repost it with Scourge named cleanly. A blank merged listing is just asking people to ignore it.",
            "Good catch. Put Scourge in the LFG line by itself so it does not look broken.",
            "Right, that listing reads wrong. Repost it with Scourge clear in the description.",
        ]
    )


def is_mixed_tunnel_or_town_plan_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    has_tunnel_option = bool(re.search(r"\b(?:tun+e?ls?|catacombs?|scou?rge|beneath|below)\b", lowered))
    has_town_option = bool(re.search(r"\b(?:shops?|shopping|vendors?|merchants?|city|town|checking\s+out)\b", lowered))
    asks_choice = bool(re.search(r"\b(?:or|either|plan(?:ned)?|today|what|wanna|want|should|could)\b", lowered))
    return has_tunnel_option and has_town_option and asks_choice


def azele_mixed_tunnel_or_town_plan_reply(message: str) -> str:
    return first_fresh_reply(
        [
            "Shops first, then Scourge Beneath if we still feel sharp. I like a plan that lets me look good before getting dusty.",
            "We can check the shops, then decide if Maz Scourgeheart deserves another visit. That feels sensible.",
            "City shops first sounds nice. After that, tunnels if you still want trouble.",
        ]
    )


def is_scourge_beneath_run_context(message: str, event: TelemetryEvent | None = None) -> bool:
    lowered = readable_game_text(message).lower()
    if is_mixed_tunnel_or_town_plan_context(lowered):
        return False
    if re.search(r"\b(?:the\s+)?scou?rge\s+(?:beneath|below)\b|\bmaz\s+scourgeheart\b", lowered):
        return True
    if event is not None and re.search(r"\b(?:do|run|another|wanna|want|go|try|scou?rge)\b.*\bscou?rge\b|\bscou?rge\b.*\b(?:run|again|wanna|want|go|try)\b", lowered):
        return True
    if not re.search(r"\b(?:another\s+)?tun+e?ls?\s+run\b|\brun\s+(?:the\s+)?tun+e?ls?\b|\btun+e?ls?\s+again\b", lowered):
        return False
    if event is None:
        return False
    map_name = map_area_label(event).lower()
    return bool(
        event.active_quest_id == 1456
        or event.map_id == 779
        or "piken square" in map_name
        or "forsaken tunnels" in map_name
    )


def azele_scourge_beneath_reply(message: str) -> str:
    return first_fresh_reply(
        [
            "Another Scourge Beneath run? Yeah. Maz Scourgeheart is not getting time to build that elemental army.",
            "For Scourge Beneath? I’m in. Forsaken Tunnels again, but careful pulls this time.",
            "Yeah, another run. Devona wants Maz Scourgeheart stopped, and honestly so do I.",
        ]
    )


def context_mentions_duke_gaban(recent_context: str | None = None) -> bool:
    haystack = recent_context or "\n".join(
        [
            *list(world_state.recent_chat_history)[-6:],
            *list(recent_reply_texts)[-6:],
        ]
    )
    return bool(re.search(r"\b(?:duke\s+)?gaban\b|\bascalonian noble\b", haystack, re.IGNORECASE))


def is_duke_gaban_search_context(message: str, event: TelemetryEvent | None = None, recent_context: str | None = None) -> bool:
    lowered = readable_game_text(message).lower()
    explicit_gaban = bool(re.search(r"\b(?:duke\s+)?gaban\b|\bascalonian noble\b", lowered))
    searchish = bool(re.search(r"\b(?:where|spots?|might|could|think|find|look|search|hide|hiding|somewhere|save|escort)\b", lowered))
    if explicit_gaban and searchish:
        return True
    map_name = map_area_label(event).lower() if event is not None else ""
    catacombs_context = "catacombs" in lowered or "catacombs" in map_name or (event is not None and event.map_id in {145, 151})
    if explicit_gaban:
        return catacombs_context
    pronoun_followup = bool(re.search(r"\b(?:he|him|his)\b", lowered))
    return bool(pronoun_followup and searchish and catacombs_context and context_mentions_duke_gaban(recent_context))


def azele_duke_gaban_search_reply(message: str) -> str:
    lowered = readable_game_text(message).lower()
    if re.search(r"\bwhere\b", lowered):
        return first_fresh_reply(
            [
                "He is in the Catacombs somewhere. I would sweep side chambers and dead ends, then be ready to escort him out.",
                "For Duke Gaban, check the side paths and little chambers first. Nobles do love getting stuck somewhere awkward.",
                "Somewhere in these Catacombs. We should search the side rooms, not just barrel down the main path.",
            ]
        )
    return first_fresh_reply(
        [
            "If he is hiding down here, I would check side chambers, alcoves, and any dead-end path before pushing deeper.",
            "Start with side rooms and corners off the main route. If Gaban is scared, he will not be standing in the open.",
            "I would sweep the side passages first. Find Gaban, then keep him close on the escort out.",
        ]
    )


def is_tunnel_plan_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    if is_duke_gaban_search_context(lowered):
        return False
    return bool(re.search(r"\b(?:tunnels?|catacombs|forsaken tunnels|scourge beneath)\b", lowered))


def is_recent_failure_bailout_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    return bool(
        re.search(r"\b(?:didn'?t work|did not work|not work out|work out|bail(?:ed)?|back out|had to leave|wipe(?:d)?)\b", lowered)
        and re.search(r"\b(?:that|it|we|us|out)\b", lowered)
    )


def azele_tunnel_plan_reply(message: str) -> str:
    lowered = readable_game_text(message).lower()
    if is_recent_failure_bailout_context(lowered) or re.search(r"\b(?:bail|didn'?t work|did not work|not work|failed|rough|bad|wipe|wiped)\b", lowered):
        return first_fresh_reply(
            [
                "Yeah, that got ugly fast. Back out, reset, then we try smarter.",
                "Agreed. That was not worth forcing. We regroup and go in cleaner next time.",
                "Fine, we bail. I like winning more than pretending a bad push is heroic.",
            ]
        )
    if re.search(r"\b(?:solo|alone|by ourselves|just us)\b", lowered):
        return first_fresh_reply(
            [
                "Solo tunnels is risky, but I’m game if we pull carefully.",
                "Just us in the tunnels? Fine, but we do this slowly and do not get surrounded.",
                "We can try it solo. You pull, I watch the messy edges.",
            ]
        )
    return first_fresh_reply(
        [
            "Alright, tunnels then. Keep it tight and do not let them wrap around us.",
            "Tunnels it is. I’ll stay sharp; you pick the first pull.",
            "Okay. Into the tunnels, but carefully. I do not want a stupid death down there.",
        ]
    )


def is_pet_evolution_context(message: str) -> bool:
    lowered = readable_game_text(message).lower()
    return bool(
        re.search(r"\b(?:pet|animal|stalker|warthog|wolf|bear)\b", lowered)
        and re.search(r"\b(?:dire|hearty|elder|develop|evolve|evolution|level)\b", lowered)
    )


def azele_pet_evolution_reply(message: str) -> str:
    return first_fresh_reply(
        [
            "Pet evolution is around level 11, depending how it fights. Dire hits harder; Hearty gets sturdier.",
            "Usually level 11 is when that starts showing. If we want Dire, let it do more of the killing.",
            "Around level 11. Hearty means tougher, Dire means meaner. Depends how we raise it.",
        ]
    )


def azele_ldoa_reply(message: str) -> str:
    if re.search(r"\b(?:aware|know|knowledge|stuff|context)\b", message, re.IGNORECASE):
        return (
            "Yes. I know the shape of it better now: stay in Ascalon, build toward level 20, "
            "use Langmar dailies, and be careful with quest rewards if we are optimizing."
        )
    return first_fresh_reply(
        [
            "For LDoA in pre, we pace it: skills, useful gear, Langmar dailies, then level 20.",
            "Defender is the long Ascalon plan. We do not leave, we build carefully, and we use Vanguard work when it matters.",
            "If we are serious about Defender, I am with you. We plan rewards, watch the Northlands, and do not waste the grind.",
        ]
    )


def azele_npc_dialogue_reply(event: TelemetryEvent) -> str:
    message = readable_game_text(event.message).lower()
    if "charr" in message:
        return first_fresh_reply(
            [
                "See? Not just me being dramatic. We should be ready.",
                "If Charr are involved, I’m listening. Keep your eyes open.",
                "That sounds like our problem soon enough.",
            ]
        )
    if any(word in message for word in ("help", "please", "trouble", "danger")):
        return first_fresh_reply(
            [
                "That sounds like someone needs something. I can work with that.",
                "I heard that too. We should at least look.",
                "Trouble, then. Because of course.",
            ]
        )
    if any(word in message for word in ("reward", "gold", "payment", "coin")):
        return first_fresh_reply(
            [
                "Reward, did they say? Now I’m listening.",
                "Finally, someone speaking practically.",
                "If there’s pay involved, I’m suddenly very attentive.",
            ]
        )
    return first_fresh_reply(
        [
            "I heard that. Not sure I like the sound of it.",
            "Well, that sounded important. Maybe.",
            "Did you catch that too, or am I being nosy?",
        ]
    )


def azele_item_drop_reply(event: TelemetryEvent) -> str:
    message = readable_game_text(event.message)
    source_name = readable_game_text(getattr(event, "agent_name", ""))
    if re.search(r"\bblack dye\b", message, re.IGNORECASE):
        if source_name:
            return f"Black Dye? In pre-Searing? That is ridiculous luck. Looked like it came from {source_name}."
        return "Black Dye? In pre-Searing? That is ridiculous luck. I did not see what dropped it."
    rarity_match = re.search(r"\b(purple|gold|green)\s+rarity\b", message, re.IGNORECASE)
    if rarity_match:
        rarity = rarity_match.group(1).title()
        if source_name:
            return f"{rarity} out here? That is worth a look. I think it dropped from {source_name}."
        return f"{rarity} out here? That is worth a look. What did it roll?"
    return first_fresh_reply(
        [
            "That drop is worth checking. What did we get?",
            "Hold on, that looked useful. Pick it up.",
            "Finally, something worth stopping for. Check it.",
        ]
    )


def azele_gw1_context_reply(context: ResolvedGameContext, event: TelemetryEvent) -> str | None:
    if not context.matched:
        return None
    message = readable_game_text(event.message).lower()
    if context.entry_id == "quest.scourge_beneath":
        if re.search(r"\bmaz\b", message) and not is_scourge_beneath_run_context(message, event):
            return first_fresh_reply(
                [
                    "Maz Scourgeheart. The necromancer stirring up trouble in the tunnels.",
                    "Maz. The one Devona wants stopped before that elemental mess gets worse.",
                    "Maz Scourgeheart, down in the Forsaken Tunnels. Not exactly harmless.",
                ]
            )
        if not is_scourge_beneath_run_context(message, event):
            return None
        return azele_scourge_beneath_reply(message)
    if context.entry_id == "title.ldoa":
        return azele_ldoa_reply(message)
    if context.entry_id == "enemy.charr" and CHARR_MENTION_PATTERN.search(message):
        return azele_charr_intent_reply(event) or "Charr threaten Ascalon. We get ready, then we hit them hard."
    if context.entry_id == "loot.black_dye":
        source_name = readable_game_text(getattr(event, "agent_name", ""))
        if source_name:
            return f"Black Dye? In pre-Searing? That is ridiculous luck. Looked like it came from {source_name}."
        return "Black Dye in pre-Searing is huge. I did not see what dropped it, though."
    if context.entry_id == "loot.purple":
        if "hammer" in message:
            return first_fresh_reply(
                [
                    "Purple hammer? Nice. In pre, that is absolutely worth checking.",
                    "Yeah, a purple hammer is a real find here. Show me the damage and mod.",
                    "Nice. Purple hammer in the Northlands is not something I ignore.",
                ]
            )
        return first_fresh_reply(
            [
                "Purple out here? That is worth checking. What did it roll?",
                "A purple in pre is actually exciting. Show me what it is.",
                "Purple? Good. Pick it up before I start staring at it.",
            ]
        )
    if context.entry_id == "item.red_iris":
        return first_fresh_reply(
            [
                "Red iris for bag space, right? Worth the little detour.",
                "A flower errand for more room is still practical. Lead on.",
                "Yeah, red irises. Pretty and useful, which I appreciate.",
            ]
        )
    if context.entry_id == "gear.krytan_leggings":
        return first_fresh_reply(
            [
                "Krytan leggings are an upgrade and a style change. I know, complicated.",
                "Better protection with the longer skirt? Fine. Practical can still look good.",
                "If the Krytan leggings are better, I’ll wear them. I may complain about the skirt length.",
            ]
        )
    if context.entry_id == "npc.devona_pet":
        return azele_devona_pet_reply(message)
    if context.entry_id == "quest.vanguard_rescue_gaban":
        return azele_duke_gaban_search_reply(message)
    return None


def azele_under_attack_reply(event: TelemetryEvent) -> str:
    hp = float(getattr(event, "player_hp", 0) or 0)
    hp_drop = float(getattr(event, "player_hp_drop", 0) or 0)
    threshold = readable_game_text(getattr(event, "hp_threshold_crossed", ""))
    severity = readable_game_text(getattr(event, "damage_severity", "")).lower()
    hp_text = f"{hp:.0%}" if hp else "low"

    if hp and hp <= 0.20:
        return first_fresh_reply(
            [
                f"I’m nearly down. {hp_text}. Get them off me.",
                f"{hp_text}. I need cover now.",
                f"Too low. {hp_text}. Help me finish this.",
            ]
        )
    if hp and (hp <= 0.35 or severity == "critical" or threshold in {"35", "35%", "20", "20%"}):
        return first_fresh_reply(
            [
                f"I’m hurting. {hp_text}. Stay on them.",
                f"{hp_text}. I need a little room.",
                f"That hit hurt. {hp_text}. Cover me.",
            ]
        )
    if hp_drop >= 0.12 or severity == "heavy":
        return first_fresh_reply(
            [
                f"Ow. Big hit. {hp_text}.",
                f"That one landed. {hp_text}. Keep pressure on.",
                f"I felt that. {hp_text}.",
            ]
        )
    if hp:
        return first_fresh_reply(
            [
                f"Ow. {hp_text}. I’m hit, but still up.",
                f"Taking hits. {hp_text}. Stay close.",
                f"I’m getting clipped. {hp_text}. Cover me.",
            ]
        )
    return first_fresh_reply(
        [
            "Ow. I’m getting hit. Help me out.",
            "I’m taking hits here.",
            "Need a hand. I’m getting hit.",
        ]
    )


def azele_status_effect_reply(event: TelemetryEvent) -> str:
    effect_type = readable_game_text(getattr(event, "effect_type", "")).lower()
    effect_name = readable_game_text(getattr(event, "effect_name", "")).lower()
    label = effect_name or effect_type

    if "bleed" in label:
        return first_fresh_reply(
            [
                "I’m bleeding. Keep them off me a second.",
                "Bleeding. Great. Don’t let them pile on me.",
                "I’m bleeding, but I can still move. Stay close.",
            ]
        )
    if "daze" in label:
        return first_fresh_reply(
            [
                "Dazed. Ugh, my head. Cover me.",
                "I’m dazed. Give me a breath.",
                "That dazed me. Keep pressure while I shake it off.",
            ]
        )
    if "blind" in label:
        return first_fresh_reply(
            [
                "Blinded. That is extremely annoying.",
                "I’m blinded. Call targets clearly.",
                "Blind on me. I’ll manage, but stay sharp.",
            ]
        )
    if "poison" in label:
        return first_fresh_reply(
            [
                "Poisoned. Lovely. Let’s end this quickly.",
                "I’m poisoned. Keep moving.",
                "Poison on me. I don’t love that.",
            ]
        )
    if "burn" in label:
        return first_fresh_reply(
            [
                "Burning. Ow. Very funny, universe.",
                "I’m burning. Finish them before I start smoking.",
                "Burning on me. Keep them busy.",
            ]
        )
    if "weak" in label or "cracked" in label:
        return first_fresh_reply(
            [
                "Weakness on me. I can still fight.",
                "I’m weakened. Don’t give them space.",
                "Weakness. Fine. We hit cleaner.",
            ]
        )
    if "hex" in label:
        return first_fresh_reply(
            [
                "I’m hexed. I hate that feeling.",
                "Hex on me. Watch for the follow-up.",
                "I’m hexed. Keep them off me while I work through it.",
            ]
        )
    if "condition" in label:
        return first_fresh_reply(
            [
                "Condition on me. Keep an eye out.",
                "I caught a condition. Still up.",
                "Something’s on me. Don’t let them press it.",
            ]
        )
    return first_fresh_reply(
        [
            "Something’s on me. Keep an eye out.",
            "I caught something nasty. Still fighting.",
            "That effect landed. Stay close.",
        ]
    )


def azele_combat_over_reply(event: TelemetryEvent) -> str:
    dead_count = int(getattr(event, "dead_hostile_count", 0) or 0)
    if dead_count >= 2:
        return first_fresh_reply(
            [
                f"That’s {dead_count} down. You still in one piece?",
                f"Group’s down. {dead_count} of them. Need a second?",
                f"That’s the group handled. {dead_count} down, and I’m still breathing.",
            ]
        )
    return first_fresh_reply(
        [
            "That’s them down. You still in one piece?",
            "Clean enough. Need a second before we move?",
            "Fight’s over. I’m still here, so I’m calling that a win.",
        ]
    )


def ollama_generate_visible(
    prompt: str,
    *,
    timeout_seconds: float | None = None,
    num_predict: int | None = None,
) -> str:
    url = settings.ollama_host.rstrip("/") + "/api/generate"
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.5,
            "top_p": 0.85,
            "repeat_penalty": 1.18,
            "repeat_last_n": 128,
            "num_ctx": min(settings.ollama_num_ctx, 4096),
            "num_predict": settings.ollama_num_predict if num_predict is None or num_predict <= 0 else num_predict,
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = settings.ollama_timeout_seconds if timeout_seconds is None or timeout_seconds <= 0 else timeout_seconds
    with ollama_request_lock:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    return str(data.get("response") or "")


def warm_ollama_model_once() -> None:
    url = settings.ollama_host.rstrip("/") + "/api/generate"
    payload = {
        "model": settings.ollama_model,
        "prompt": "/no_think\n.",
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.0,
            "num_ctx": 1024,
            "num_predict": 1,
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started_at = time.perf_counter()
    if not ollama_request_lock.acquire(blocking=False):
        print("Ollama model warm skipped because another Hermes request is active.", flush=True)
        return
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response.read()
    finally:
        ollama_request_lock.release()
    print(
        f"Ollama model warm completed in {time.perf_counter() - started_at:.2f}s "
        f"(model={settings.ollama_model}).",
        flush=True,
    )


async def ollama_keepalive_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(warm_ollama_model_once)
        except Exception as exc:
            print(f"Ollama model warm failed ({type(exc).__name__}: {exc}).", flush=True)
        await asyncio.sleep(20 * 60)


AMBIENT_SCHEDULER_METAPHOR_PATTERN = re.compile(
    r"\b(?:heart\s*beat|pulse|pulses|pulsing|rhythm|rhythms)\b",
    re.IGNORECASE,
)

VISIBLE_SELF_MANAGEMENT_PATTERN = re.compile(
    r"\b(?:resetting(?:\s+now)?|new line|system reset|resetting myself|retrying|regenerating)\b",
    re.IGNORECASE,
)


def leaks_ambient_scheduler_metaphor(reply: str, event: TelemetryEvent) -> bool:
    if not is_ambient_snapshot_event(event):
        return False
    return bool(AMBIENT_SCHEDULER_METAPHOR_PATTERN.search(reply or ""))


def leaks_visible_self_management(reply: str) -> bool:
    return bool(VISIBLE_SELF_MANAGEMENT_PATTERN.search(reply or ""))


def validate_model_reply(reply: str, event: TelemetryEvent) -> str:
    reply = repair_model_reply(reply)
    if leaks_ambient_scheduler_metaphor(reply, event):
        raise ValueError("leaked ambient scheduler metaphor")
    if leaks_visible_self_management(reply):
        raise ValueError("leaked self-management phrase")
    if re.search(r"\b(kid|tasty|elemental fun|dance in flames)\b", reply, re.IGNORECASE):
        raise ValueError("bad style model reply")
    if FILLER_ONLY_REPLY_PATTERN.search(reply):
        raise ValueError("filler-only model reply")
    if LOW_QUALITY_REPLY_PATTERNS.search(reply):
        raise ValueError("low quality model reply")
    if is_azele_wearable_context(event.message) and misdirects_wearable_to_player(reply):
        raise ValueError("misdirected wearable ownership")
    if invents_unsupported_rumor(reply, event):
        raise ValueError("unsupported rumor reference")
    if invents_unsupported_ashford_reference(reply, event):
        raise ValueError("unsupported Ashford reference")
    if invents_ambient_loot_hook(reply, event):
        raise ValueError("unsupported ambient loot reference")
    if invents_nicholas_sandford_request(reply, event):
        raise ValueError("unsupported Nicholas Sandford request")
    if invents_fort_ranik_northlands_route(reply, event):
        raise ValueError("unsupported Fort Ranik/Northlands route")
    if invents_level_up_pack_causality(reply, event):
        raise ValueError("unsupported level-up pack causality")
    if misses_clear_player_intent(reply, event):
        raise ValueError("missed clear player intent")
    if misdirects_voice_preference(reply, event):
        raise ValueError("misdirected voice reply")
    if reply_too_long_for_context(reply, event):
        raise ValueError("overlong model reply")
    if invents_self_duplicate(reply):
        raise ValueError("unsupported self duplicate reference")
    if model_reply_has_bad_shape(reply):
        raise ValueError("bad shape model reply")
    if is_too_similar_to_recent_replies(reply):
        raise ValueError("repeated recent reply")
    if not reply:
        raise ValueError("empty model reply")
    return reply


def salvage_complete_model_reply(reply: str, event: TelemetryEvent) -> str | None:
    cleaned = repair_model_reply(reply)
    if not cleaned:
        return None
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    if len(sentences) <= 1:
        return None

    best_valid: str | None = None
    prefix: list[str] = []
    for sentence in sentences:
        prefix.append(sentence)
        candidate = " ".join(prefix).strip()
        if candidate == cleaned:
            break
        if not re.search(r"[.!?]$", candidate):
            continue
        try:
            best_valid = validate_model_reply(candidate, event)
        except Exception:
            continue
    return best_valid


def repair_model_reply(reply: str) -> str:
    cleaned = re.sub(r"\s+", " ", reply or "").strip()
    cleaned = re.sub(r"\bWhat['’]?ve\s+got\b", "What's got", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bwhat['’]?ve\s+got\b", "what's got", cleaned)
    cleaned = re.sub(r"\bHey myself too[,.]?\s*", "Hey. Me too. ", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def character_reply_with_ollama(
    event: TelemetryEvent,
    *,
    timeout_seconds: float | None = None,
    num_predict: int | None = None,
    record_id: int | None = None,
) -> HermesDecision:
    started_at = time.perf_counter()
    base_prompt = build_character_reply_prompt(event)
    response = ollama_generate_visible(base_prompt, timeout_seconds=timeout_seconds, num_predict=num_predict)
    elapsed = time.perf_counter() - started_at
    print(
        f"Ollama character reply generated in {elapsed:.2f}s "
        f"(model={settings.ollama_model}, ctx={settings.ollama_num_ctx}, predict={num_predict or settings.ollama_num_predict}).",
        flush=True,
    )
    cleaned = clean_model_reply(response)
    try:
        reply = validate_model_reply(cleaned, event)
    except Exception as exc:
        preview = clamp_gw_chat_line(cleaned)[:160]
        print(
            f"Ollama character reply rejected for {event_debug_label(event, record_id=record_id)} "
            f"({type(exc).__name__}: {exc}): {preview!r}",
            flush=True,
        )
        if str(exc) in {
            "missed clear player intent",
            "bad shape model reply",
            "repeated recent reply",
            "overlong model reply",
            "misdirected voice reply",
            "leaked ambient scheduler metaphor",
            "leaked self-management phrase",
        }:
            retry_exc: Exception | None = None
            retry_started_at = time.perf_counter()
            retry_prompt = build_player_intent_retry_prompt(event, cleaned, str(exc))
            try:
                retry_response = ollama_generate_visible(retry_prompt, timeout_seconds=timeout_seconds, num_predict=num_predict)
                retry_elapsed = time.perf_counter() - retry_started_at
                retry_cleaned = clean_model_reply(retry_response)
                retry_reply = validate_model_reply(retry_cleaned, event)
                print(
                    f"Ollama character reply intent retry accepted in {retry_elapsed:.2f}s "
                    f"for {event_debug_label(event, record_id=record_id)}.",
                    flush=True,
                )
                return HermesDecision(
                    should_speak=True,
                    channel_override="CHANNEL_PARTY",
                    urgency="NORMAL",
                    response=retry_reply,
                )
            except Exception as retry_error:
                retry_exc = retry_error
                print(
                    f"Ollama character reply retry rejected for {event_debug_label(event, record_id=record_id)} "
                    f"({type(retry_error).__name__}: {retry_error}).",
                    flush=True,
                )

            salvage = salvage_complete_model_reply(cleaned, event)
            if salvage:
                print(
                    f"Ollama character reply salvaged complete prefix after retry failure "
                    f"for {event_debug_label(event, record_id=record_id)}.",
                    flush=True,
                )
                return HermesDecision(
                    should_speak=True,
                    channel_override="CHANNEL_PARTY",
                    urgency="NORMAL",
                    response=salvage,
                )
            if retry_exc is not None:
                raise retry_exc
        raise
    return HermesDecision(
        should_speak=True,
        channel_override="CHANNEL_PARTY",
        urgency="NORMAL",
        response=reply,
    )


def decide_with_ollama(event: TelemetryEvent, *, record_id: int | None = None) -> HermesDecision:
    if should_use_direct_character_reply(event):
        if event.event_type == "player_chat" and event.channel == "party":
            return character_reply_with_ollama(
                event,
                timeout_seconds=settings.hermes_player_chat_ollama_timeout_seconds,
                num_predict=settings.hermes_player_chat_ollama_num_predict,
                record_id=record_id,
            )
        return character_reply_with_ollama(event, record_id=record_id)

    import ollama

    prompt = build_decision_prompt(event)
    response = ollama.generate(
        model=settings.ollama_model,
        prompt=prompt,
        format="json",
        options={
            "temperature": 0.2,
            "num_ctx": settings.ollama_num_ctx,
            "num_predict": settings.ollama_num_predict,
        },
    )
    raw = response.get("response", "{}")
    return HermesDecision.model_validate(extract_json_object(raw))


def fallback_rule_decision(event: TelemetryEvent) -> HermesDecision:
    if event.event_type in {"player_chat", "chat_log"} and event.channel == "party":
        persona = event.persona.strip()
        if persona.lower() == "azele":
            response = azele_fast_reply(event)
        else:
            response = "I’m with you. Say the word and I’ll keep watch."
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="NORMAL",
            response=clamp_gw_chat_line(response),
        )
    if is_npc_dialogue_event(event):
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response=clamp_gw_chat_line(azele_npc_dialogue_reply(event)),
        )
    if is_ambient_snapshot_event(event):
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response=clamp_gw_chat_line(ambient_quip(event)),
        )
    if event.event_type == "item_drop":
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="NORMAL",
            response=clamp_gw_chat_line(azele_item_drop_reply(event)),
        )
    if event.event_type == "party_member_down":
        name = readable_game_text(getattr(event, "agent_name", ""))
        response = f"{name} is down. Move, I can cover." if name else "Someone's down. Move, I can cover."
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="HIGH",
            response=clamp_gw_chat_line(response),
        )
    if event.event_type == "environment_alert":
        if not should_speak_for_environment_alert(event):
            return HermesDecision(should_speak=False)
        if event.alert_type == "under_attack":
            response = azele_under_attack_reply(event)
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="HIGH",
                response=clamp_gw_chat_line(response),
            )
        if event.alert_type == "status_effect":
            response = azele_status_effect_reply(event)
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="HIGH",
                response=clamp_gw_chat_line(response),
            )
        if event.alert_type == "combat_over":
            response = azele_combat_over_reply(event)
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="NORMAL",
                response=clamp_gw_chat_line(response),
            )
        if event.alert_type == "party_member_down":
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="HIGH",
                response=clamp_gw_chat_line("Someone's down. Move, I can cover."),
            )
        if event.alert_type == "combat_started":
            target_name = readable_game_text(event.agent_name)
            if target_name:
                response = f"On {target_name}. Stay close."
            else:
                response = "On that one. Stay close."
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="HIGH",
                response=clamp_gw_chat_line(response),
            )
        if event.alert_type == "danger_spike":
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="HIGH",
                response=clamp_gw_chat_line(f"{event.close_hostile_count} enemies close. Stay with me."),
            )
        return HermesDecision(should_speak=False)
    if event.event_type in MAP_COMMENT_EVENT_TYPES:
        response = rotating_map_comment(event)
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="LOW",
            response=clamp_gw_chat_line(response),
        )
    if event.event_type == "active_quest_changed":
        return HermesDecision(should_speak=False)
    if event.event_type == "target_changed":
        hp = event.player_hp
        target_name = readable_game_text(getattr(event, "agent_name", ""))
        if not target_name and not has_visible_enemy_context(event):
            return HermesDecision(should_speak=False)
        if target_name and event.hostile_count <= 0 and event.close_hostile_count <= 0:
            response = f"I see {target_name}."
        elif hp and hp < 0.35:
            response = "Low already. Finish it."
        elif target_name:
            response = f"On {target_name}."
        else:
            response = "On that one."
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="NORMAL",
            response=clamp_gw_chat_line(response),
        )
    return HermesDecision(should_speak=False)


def azele_fast_reply(event: TelemetryEvent) -> str:
    message = (event.message or "").lower()
    if is_voice_preference_context(message):
        return azele_voice_preference_reply(message)
    if combat_reply := azele_recent_combat_reply(event):
        return combat_reply
    if social_reply := (azele_social_banter_reply(message) if is_social_banter_context(message) else None):
        return social_reply
    if is_player_checkin(message):
        return azele_player_checkin_reply(message)
    if clarification := azele_clarification_reply(message):
        return clarification
    if is_scourge_lfg_context(message):
        return azele_scourge_lfg_reply(message)
    if is_mixed_tunnel_or_town_plan_context(message):
        return azele_mixed_tunnel_or_town_plan_reply(message)
    if is_scourge_beneath_run_context(message, event):
        return azele_scourge_beneath_reply(message)
    if contextual_followup := azele_contextual_followup_reply(message):
        return contextual_followup
    if simple_ack := azele_simple_ack_reply(message):
        return simple_ack
    if charr_reply := azele_charr_intent_reply(event):
        return charr_reply
    if gw1_reply := azele_gw1_context_reply(resolve_gw1_context(event, recent_conversation_context(limit=6, persona=event.persona)), event):
        return gw1_reply
    quest = readable_game_text(event.active_quest_name)
    if is_pet_evolution_context(message):
        return azele_pet_evolution_reply(message)
    if is_recent_failure_bailout_context(message):
        return azele_tunnel_plan_reply(message)
    if is_tunnel_plan_context(message):
        return azele_tunnel_plan_reply(message)
    if is_ldoa_context(message):
        return azele_ldoa_reply(message)
    if is_alcohol_consumable_context(message):
        return first_fresh_reply(
            [
                "Fifteen Dwarven Ale? Warm, loud, and a little betrayed you counted.",
                "I feel like Ascalon City is leaning sideways. Happy now?",
                "Fifteen? I feel brave, sparkly, and absolutely unfit for stairs.",
            ]
        )
    if is_fort_ranik_northlands_correction(message):
        return first_fresh_reply(
            [
                "You’re right. I mixed that up. Fort Ranik is south; Northlands is a different problem.",
                "Right, my mistake. Fort Ranik is south of here, not in the Northlands.",
                "Yeah, you’re right. I got the route tangled. Fort Ranik is south.",
            ]
        )
    if is_nicholas_sandford_context(message):
        return first_fresh_reply(
            [
                "Right. Nicholas wants five of one specific daily trophy for a Gift of the Huntsman.",
                "Exactly, just his daily item. Save that, not random weapons or potions.",
                "Yeah. Nicholas is picky: five of whatever trophy he is asking for today.",
            ]
        )
    if is_devona_pet_context(message):
        return azele_devona_pet_reply(message)
    if is_level_up_congratulations_context(message):
        return first_fresh_reply(
            [
                "Thanks. Level 14 feels good. Now I actually feel ready for the Northlands.",
                "Thank you. I know it is just one level, but I feel stronger already.",
                "We made it. Level 14 sounds much better when you say it like that.",
            ]
        )
    if is_level_charr_context(message):
        return first_fresh_reply(
            [
                "Level 14, then Charr past the gate. Good. We defend Ascalon.",
                "Yes. Get me to 14, then we make the Charr regret coming near Ascalon.",
                "That makes sense. One more push, then we head past the Wall together.",
            ]
        )
    if is_red_iris_bag_context(message):
        return first_fresh_reply(
            [
                "One more iris for more bag space? Good. That is worth the detour.",
                "Yeah, extra slots are worth chasing a flower for. Lead me to it.",
                "A red iris for bag space is practical enough that I cannot complain.",
            ]
        )
    if "small equipment pack" in message:
        return first_fresh_reply(
            [
                "Only weapons and armor? Annoying, but still useful. More room is more room.",
                "That is weirdly specific, but fine. Weapons and armor can stop clogging the main bag.",
                "So it is gear-only storage. Still useful, just less magical than I hoped.",
            ]
        )
    if re.search(r"\b(?:five|5)\s+(?:more\s+)?(?:bag\s+)?slots?\b", message) or re.search(r"\bmore space for items?\b", message):
        return first_fresh_reply(
            [
                "Five more slots is huge. That actually changes how long we can stay out.",
                "Yeah, more item space matters. Less running back every five minutes.",
                "That is a real upgrade. More space means we can be pickier later.",
            ]
        )
    if any(phrase in message for phrase in {"you ok", "you okay", "are you ok", "are you okay", "u ok", "you better"}):
        return first_fresh_reply(
            [
                "Yeah, I’m okay. That came out weird. Don’t make a thing of it.",
                "I’m fine. My mouth just did something stupid, apparently.",
                "Yeah. Ignore that one, it came out wrong.",
            ]
        )
    if re.search(r"\b(locked and loaded|ready with that|had that ready|had to.*loaded|loaded,? didn't you)\b", message):
        return first_fresh_reply(
            [
                "Maybe. I like being ready before you make it sound like a dare.",
                "A little. You left the opening right there.",
                "Yes, and you walked straight into it. That one is on you.",
            ]
        )
    if "of course" in message or "obviously" in message:
        return first_fresh_reply(
            [
                "Yeah. You know me. Keep up.",
                "Pretty much. Don’t act surprised.",
                "Exactly. Glad we’re on the same page.",
            ]
        )
    if "brat" in message:
        return first_fresh_reply(
            [
                "Maybe a little. You seem fine with it.",
                "Only sometimes. Don’t act surprised.",
                "A little, yeah. It’s not my worst quality.",
            ]
        )
    if "opening" in message:
        return first_fresh_reply(
            [
                "If there’s an opening, take it.",
                "Good. Let’s not waste it.",
                "That works. Move before it closes.",
            ]
        )
    if re.search(r"\b(look good|pretty|beautiful|cute|hot)\b", message):
        return first_fresh_reply(
            [
                "I know. Still nice to hear.",
                "Thanks. I did put effort in, obviously.",
                "You noticed. Good, keep doing that.",
            ]
        )
    if is_azele_style_tease_context(message):
        return azele_style_tease_reply(message)
    if is_skirt_outfit_question(message):
        if re.search(r"\b(prefer|which|long or short|short or long)\b", message):
            return first_fresh_reply(
                [
                    "Shorter, honestly. But if the Krytan one protects better, I can behave.",
                    "I like the mini skirt more. The longer one sounds practical, annoyingly.",
                    "Short skirt for looks, longer skirt if we expect trouble. See? Balanced.",
                ]
            )
        return first_fresh_reply(
            [
                "Right, so it changes the look too. Longer skirt, less showing off.",
                "Ah, I get it. Better gear, but a more covered look.",
                "So it is an upgrade and a style change. That makes it harder.",
            ]
        )
    if re.search(r"\b(upgrade|armor|armour|leggings?|krytan|collector|collecting)\b", message):
        if "legging" in message or "krytan" in message:
            return first_fresh_reply(
                [
                    "Krytan leggings? If they’re an upgrade, yes. Let me try them.",
                    "That is useful. Better leggings now means fewer bruises later.",
                    "Good find. If they fit better than these, I’m not arguing.",
                ]
            )
        return first_fresh_reply(
            [
                "If it’s an upgrade, take it. Looking good and staying alive can both happen.",
                "That sounds useful. Let’s not walk past better gear.",
                "Good. Better gear first, then we can pretend we planned ahead.",
            ]
        )
    if re.search(r"\b(inventory|bags?|sell|salvage|merchant|storage|gear|slots?|space|pack)\b", message):
        return first_fresh_reply(
            [
                "Good. Clear the bags first, then we can stay out longer.",
                "That makes sense. More space means less stopping when things get good.",
                "Practical. I like it when preparation actually buys us time.",
            ]
        )
    if re.search(r"\b(stage|althea|iris|irises|flower|flowers)\b", message):
        if "stage" in message or "althea" in message:
            return first_fresh_reply(
                [
                    "Althea's stage, right. Good eye. Let's check around it.",
                    "Yeah, her stage is a sensible place to look. Pretty enough for irises.",
                    "That tracks. If an iris is hiding there, I want credit for agreeing.",
                ]
            )
        return first_fresh_reply(
            [
                "Red irises, then. Let's sweep the pretty spots first.",
                "Flowers first. Very heroic, obviously, but useful.",
                "Good. Small, bright, easy to miss. Keep your eyes low.",
            ]
        )
    if re.search(r"\b(?:six|6)\s+gods?\b|\b(gods?|attuned|attunement|balthazar|dwayna|grenth|lyssa|melandru|kormir)\b", message):
        return first_fresh_reply(
            [
                "Lyssa, probably. Style, illusion, a little trouble. That feels honest.",
                "Lyssa fits me best, I think. Pretty, clever, and not as harmless as she looks.",
                "For me? Lyssa. I like beauty with teeth, apparently.",
            ]
        )
    if re.search(r"\b(lead the way|you lead|go ahead|after you)\b", message):
        return first_fresh_reply(
            [
                "Gladly. Stay close and try to look like this was your idea.",
                "Alright. I’ll set the pace, you keep up.",
                "Fine by me. Watch the edges while I pick the road.",
            ]
        )
    if re.search(r"\b(with you|i'm with you|im with you|you with me|same page)\b", message):
        return first_fresh_reply(
            [
                "Good. I like hearing that before we do something reckless.",
                "Then stay close. I’m easier to follow when you admit I’m right.",
                "Good. That makes two of us, which is better odds than usual.",
            ]
        )
    if re.search(r"\b(stay alive|staying alive|survive|not dying|don't die|dont die)\b", message):
        return first_fresh_reply(
            [
                "Fair. Alive first, clever later.",
                "That is annoyingly sensible. I can work with alive.",
                "Good plan. I prefer my heroics with breathing afterward.",
            ]
        )
    if is_simple_greeting(message):
        return first_fresh_reply(
            [
                "Hey. I’m here.",
                "Hey. Good to hear you.",
                "Hi. I’m with you.",
                "There you are. What’s up?",
            ]
        )
    if re.fullmatch(r"\s*(?:gl|good luck)[.!?,\s]*", message):
        return first_fresh_reply(
            [
                "Good luck to us, then. Stay sharp.",
                "We’ll take the luck. I’ll bring the stubborn part.",
                "Thanks. Keep close and we’ll make it count.",
            ]
        )
    if re.fullmatch(r"\s*(?:gz|grats?|congrats?|congratulations)[.!?,\s]*", message):
        return first_fresh_reply(
            [
                "Thanks. I’m trying not to look too pleased about it.",
                "Thank you. That felt good, actually.",
                "Thanks. Stronger every step, right?",
            ]
        )
    if re.search(r"^(ready|go)$|\b(let'?s go|ready now|i'?m ready|im ready)\b", message):
        return first_fresh_reply(
            [
                "Ready. Stay close.",
                "Yeah. Let’s go.",
                "Go on. I’m right behind you.",
            ]
        )
    if re.search(r"\b(?:thanks|thank you|ty)\b", message):
        return first_fresh_reply(
            [
                "You’re welcome. Try not to sound too shocked.",
                "Of course. I can be useful sometimes.",
                "Anytime. Just don’t make it a habit.",
            ]
        )
    if "where" in message or "lost" in message:
        return first_fresh_reply(
            [
                "Give me a second to get my bearings.",
                "Hold on, I’m checking the road.",
                "I think I know where we are. Let me look.",
            ]
        )
    if "help" in message or "stuck" in message:
        return first_fresh_reply(
            [
                "I’m with you. Slow it down and we’ll sort this out.",
                "Alright, breathe. Tell me what went wrong.",
                "I’m here. Tell me what happened.",
            ]
        )
    if "charr" in message:
        return "They’re a threat to Ascalon. We don’t take that lightly, you know?"
    if "quest" in message and quest:
        return f"{quest}, then. Let’s keep it simple and not wander into three new problems."
    if "lol" in message or "haha" in message:
        return first_fresh_reply(
            [
                "Okay, that was a little funny. A little.",
                "Laugh all you want. I’m still right.",
                "See, now you’re encouraging me.",
            ]
        )
    if "?" in message:
        return first_fresh_reply(
            [
                "Maybe. Give me one more detail and I’ll work with it.",
                "Could be. Tell me what you’re looking at.",
                "Maybe. Point me at the part you mean.",
            ]
        )
    return first_fresh_reply(
        [
            "Yeah, I’m here. What are we doing?",
            "I’m listening. What’s up?",
            "Okay. Keep going.",
        ]
    )


def _supabase_configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_service_key)


def _audio_mime_type(audio_format: str) -> str:
    normalized = audio_format.strip().lower()
    if normalized == "wav":
        return "audio/wav"
    if normalized == "ogg":
        return "audio/ogg"
    return "audio/mpeg"


TTS_EXPRESSION_PROFILES: dict[str, dict[str, Any]] = {
    "neutral": {"tags": [], "temperature": 0.78, "exaggeration": 0.0},
    "happy": {"tags": ["[happy]", "[chuckle]"], "temperature": 0.86, "exaggeration": 0.35},
    "teasing": {"tags": ["[teasing]", "[chuckle]"], "temperature": 0.9, "exaggeration": 0.45},
    "flirty": {"tags": ["[softly]", "[chuckle]"], "temperature": 0.88, "exaggeration": 0.5},
    "confident": {"tags": ["[confident]"], "temperature": 0.8, "exaggeration": 0.25},
    "annoyed": {"tags": ["[annoyed]"], "temperature": 0.84, "exaggeration": 0.45},
    "angry": {"tags": ["[angry]"], "temperature": 0.92, "exaggeration": 0.65},
    "worried": {"tags": ["[worried]", "[gasp]"], "temperature": 0.86, "exaggeration": 0.55},
    "sad": {"tags": ["[sad]", "[sigh]"], "temperature": 0.72, "exaggeration": 0.4},
    "embarrassed": {"tags": ["[embarrassed]", "[sigh]"], "temperature": 0.82, "exaggeration": 0.4},
}


EXPRESSION_ALIASES = {
    "anger": "angry",
    "irritated": "annoyed",
    "irritation": "annoyed",
    "worry": "worried",
    "fear": "worried",
    "scared": "worried",
    "afraid": "worried",
    "playful": "teasing",
    "tease": "teasing",
    "flirt": "flirty",
    "romantic": "flirty",
    "shy": "embarrassed",
    "embarrass": "embarrassed",
}


def normalize_expression(expression: str) -> str:
    normalized = expression.strip().lower().replace("_", "-")
    normalized = EXPRESSION_ALIASES.get(normalized, normalized)
    return normalized if normalized in TTS_EXPRESSION_PROFILES else "neutral"


def tts_expression_profile(expression: str) -> dict[str, Any]:
    normalized = normalize_expression(expression)
    profile = dict(TTS_EXPRESSION_PROFILES[normalized])
    profile["expression"] = normalized
    return profile


def reply_expression(text: str, urgency: str = "NORMAL") -> str:
    lowered = readable_game_text(text).lower()
    if re.search(
        r"\b(?:charr|threat|kill|furious|angry|hate|burn|enemy|fight them|do not hesitate|hold the line|breach|advance|through (?:the )?gate)\b",
        lowered,
    ):
        return "angry"
    if re.search(r"\b(?:annoy|seriously|really\?|ugh|stop it|not funny|try not to|don'?t make me|keep up)\b", lowered):
        return "annoyed"
    if urgency.upper() == "HIGH" or re.search(r"\b(?:hit|down|move|cover|danger|careful|trouble|hurt|help|run|close)\b", lowered):
        return "angry" if "charr" in lowered else "worried"
    if re.search(r"\b(?:sorry|sad|miss|hurt|afraid|scared|worried|quiet)\b", lowered):
        return "sad"
    if re.search(r"\b(?:blush|embarrass|awkward|shut up|don'?t make a thing)\b", lowered):
        return "embarrassed"
    if re.search(r"\b(?:love|flirt|cute|hot|pretty|kiss|want me|admire|staring)\b", lowered):
        return "flirty"
    if re.search(r"\b(?:obviously|keep up|i know|impressive|ready|with you|good plan)\b", lowered):
        return "confident"
    if re.search(r"\b(?:funny|tease|brat|laugh|chuckle|sure you do|try again)\b", lowered):
        return "teasing"
    if re.search(r"\b(?:thanks|nice|good|glad|happy|finally)\b", lowered):
        return "happy"
    return "neutral"


TTS_PRONUNCIATION_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bHmph\b", re.IGNORECASE), "Humph"),
    (re.compile(r"\bAzele\b", re.IGNORECASE), "Azelle"),
    (re.compile(r"\bAscalon\b", re.IGNORECASE), "Ask-alon"),
)


def tts_pronunciation_text(text: str) -> str:
    spoken = text
    for pattern, replacement in TTS_PRONUNCIATION_REPLACEMENTS:
        spoken = pattern.sub(replacement, spoken)
    return spoken


def kokoro_tts_voice_for_persona(persona: str | None = None) -> str:
    persona_key = known_persona_name(persona or "").lower()
    if persona_key == "azele":
        return "af_heart"
    if persona_key == "meliora andru":
        return "af_bella"
    return settings.kokoro_tts_voice


def _kokoro_tts_payload(text: str, *, persona: str | None = None, voice: str | None = None) -> dict[str, Any]:
    return {
        "model": settings.kokoro_tts_model,
        "input": tts_pronunciation_text(text),
        "voice": voice or kokoro_tts_voice_for_persona(persona),
        "response_format": settings.kokoro_tts_format,
    }


def generate_kokoro_audio(text: str, *, persona: str | None = None) -> tuple[bytes, str] | None:
    if not text.strip():
        return None

    body = json.dumps(_kokoro_tts_payload(text, persona=persona)).encode("utf-8")
    request = urllib.request.Request(
        settings.kokoro_tts_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": _audio_mime_type(settings.kokoro_tts_format),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.kokoro_tts_timeout_seconds) as response:
            status = getattr(response, "status", 200)
            audio = response.read()
    except Exception as exc:
        print(f"Hermes Kokoro TTS request failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    if status < 200 or status >= 300 or not audio:
        return None
    return audio, _audio_mime_type(settings.kokoro_tts_format)


def _chatterbox_tts_payload(text: str, *, expression: str) -> dict[str, Any]:
    profile = tts_expression_profile(expression)
    payload: dict[str, Any] = {
        "input": tts_pronunciation_text(text),
        "voice_sample_path": settings.chatterbox_tts_voice_sample,
        "response_format": settings.chatterbox_tts_format,
        "expression": profile["expression"],
        "exaggeration": profile.get("exaggeration", settings.chatterbox_tts_exaggeration),
        "temperature": profile.get("temperature", settings.chatterbox_tts_temperature),
    }
    return payload


def generate_chatterbox_turbo_audio(text: str, *, expression: str) -> tuple[bytes, str] | None:
    if not text.strip():
        return None

    body = json.dumps(_chatterbox_tts_payload(text, expression=expression)).encode("utf-8")
    request = urllib.request.Request(
        settings.chatterbox_tts_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": _audio_mime_type(settings.chatterbox_tts_format),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.chatterbox_tts_timeout_seconds) as response:
            status = getattr(response, "status", 200)
            audio = response.read()
    except Exception as exc:
        print(f"Hermes Chatterbox Turbo TTS request failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    if status < 200 or status >= 300 or not audio:
        return None
    return audio, _audio_mime_type(settings.chatterbox_tts_format)


def generate_tts_audio(text: str, *, expression: str, persona: str | None = None) -> tuple[bytes, str, str, str] | None:
    provider = settings.hermes_tts_provider
    if provider in {"chatterbox-turbo", "chatterbox_turbo"}:
        generated = generate_chatterbox_turbo_audio(text, expression=expression)
        if generated:
            audio, mime_type = generated
            return audio, mime_type, "chatterbox-turbo", settings.chatterbox_tts_voice_sample
        generated = generate_kokoro_audio(text, persona=persona)
        if generated:
            audio, mime_type = generated
            return audio, mime_type, "kokoro", kokoro_tts_voice_for_persona(persona)
        return None
    if provider in {"kokoro", "kokoro-local"}:
        generated = generate_kokoro_audio(text, persona=persona)
        if generated:
            audio, mime_type = generated
            return audio, mime_type, provider, kokoro_tts_voice_for_persona(persona)
    return None


def _reply_audio_path(reply: CompanionReplyInsert, audio_format: str) -> str:
    extension = audio_format.strip().lower() or "mp3"
    if extension == "mpeg":
        extension = "mp3"
    persona = re.sub(r"[^a-zA-Z0-9_-]+", "-", reply.persona.strip().lower()).strip("-") or "persona"
    session = re.sub(r"[^a-zA-Z0-9_-]+", "-", reply.session_id.strip().lower()).strip("-") or "session"
    digest = hashlib.sha256(
        f"{reply.persona}|{reply.session_id}|{reply.trigger_log_id}|{reply.message}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{session}/{persona}/{int(time.time())}-{digest}.{extension}"


def _signed_url_value(response: Any) -> str | None:
    data = getattr(response, "data", None) or response
    if isinstance(data, dict):
        value = data.get("signedURL") or data.get("signedUrl") or data.get("signed_url")
        if isinstance(value, str):
            return value
    return None


def attach_tts_audio(reply: CompanionReplyInsert) -> CompanionReplyInsert:
    labeled_expression, visible_message = split_spoken_expression_label(reply.message)
    expression = normalize_expression(
        str(reply.metadata.get("expression") or labeled_expression or reply_expression(visible_message, reply.urgency))
    )
    profile = tts_expression_profile(expression)
    reply = reply.model_copy(
        update={
            "message": visible_message,
            "metadata": {
                **reply.metadata,
                "expression": expression,
                "tts_expression_profile": profile,
            }
        }
    )
    if settings.hermes_tts_provider not in {"kokoro", "kokoro-local", "chatterbox-turbo", "chatterbox_turbo"}:
        return reply
    if not _supabase_configured():
        return reply

    try:
        generated = generate_tts_audio(reply.message, expression=expression, persona=reply.persona)
        if not generated:
            return reply.model_copy(update={"metadata": {**reply.metadata, "tts_error": "audio_generation_unavailable"}})
        audio, mime_type, provider, voice = generated
        client = create_supabase_client(settings)
        bucket = settings.hermes_tts_storage_bucket
        path = _reply_audio_path(reply, settings.chatterbox_tts_format if provider == "chatterbox-turbo" else settings.kokoro_tts_format)
        storage = client.storage.from_(bucket)
        storage.upload(
            path=path,
            file=audio,
            file_options={
                "content-type": mime_type,
                "upsert": "true",
            },
        )
        signed = _signed_url_value(storage.create_signed_url(path, settings.hermes_tts_signed_url_seconds))
        if not signed:
            return reply
        metadata = {
            **reply.metadata,
            "audio_url": signed,
            "audio_storage_bucket": bucket,
            "audio_storage_path": path,
            "audio_mime_type": mime_type,
            "audio_expires_at": datetime.fromtimestamp(
                time.time() + settings.hermes_tts_signed_url_seconds,
                timezone.utc,
            ).isoformat(),
            "tts_provider": provider,
            "tts_voice": voice,
        }
        return reply.model_copy(update={"metadata": metadata})
    except Exception as exc:
        print(f"Hermes TTS audio unavailable: {type(exc).__name__}: {exc}", flush=True)
        return reply.model_copy(
            update={"metadata": {**reply.metadata, "tts_error": f"{type(exc).__name__}: {str(exc)[:180]}"}}
        )


def tts_audio_required() -> bool:
    return settings.hermes_tts_provider in {"kokoro", "kokoro-local", "chatterbox-turbo", "chatterbox_turbo"} and _supabase_configured()


def reply_has_audio(reply: CompanionReplyInsert) -> bool:
    return bool(reply.metadata.get("audio_url") or reply.metadata.get("audio_signed_url"))


def can_publish_text_only_reply(reply: CompanionReplyInsert) -> bool:
    return (
        str(reply.metadata.get("trigger_event_type") or "").lower() == "player_chat"
        and str(reply.metadata.get("trigger_channel") or "").lower() == "party"
    )


def insert_reply(reply: CompanionReplyInsert, *, consumed: bool = False) -> CompanionReplyInsert | None:
    if not _supabase_configured():
        return None
    reply = attach_tts_audio(reply)
    if tts_audio_required() and not reply_has_audio(reply):
        if can_publish_text_only_reply(reply):
            reply = reply.model_copy(
                update={
                    "metadata": {
                        **reply.metadata,
                        "suppress_tts": True,
                        "delivery": "text_only_tts_unavailable",
                    }
                }
            )
        else:
            print(
                f"Hermes skipped companion reply without TTS audio: {reply.metadata.get('tts_error') or 'missing audio_url'}",
                flush=True,
            )
            return None
    client = create_supabase_client(settings)
    row = reply.to_supabase_insert()
    if consumed:
        row["consumed_at"] = utc_now_iso()
        row["payload"]["delivery"] = "direct_lan"
    client.table(COMPANION_REPLIES_TABLE).insert(row).execute()
    return reply


def _parse_created_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def recent_player_chat_in_supabase(persona: str, session_id: str, now: float, quiet_seconds: float) -> bool:
    if not _supabase_configured():
        return False
    try:
        client = create_supabase_client(settings)
        response = (
            client.table(GAME_LOGS_TABLE)
            .select("id,created_at,sender,channel,payload")
            .eq("channel", "party")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
    except Exception as exc:
        print(f"Hermes recent player chat check failed: {type(exc).__name__}.", flush=True)
        return False
    persona_key = persona.strip().lower()
    for row in response.data or []:
        payload = row.get("payload") or row.get("metadata") or {}
        if payload.get("event_type") != "player_chat":
            continue
        row_persona = str(payload.get("persona") or "").strip().lower()
        if persona_key and row_persona and row_persona != persona_key:
            continue
        row_session_id = str(payload.get("session_id") or "").strip()
        if session_id and row_session_id and row_session_id != session_id:
            continue
        created_at = _parse_created_at(row.get("created_at"))
        if not created_at:
            continue
        age = now - created_at.timestamp()
        if -5.0 <= age < quiet_seconds:
            return True
    return False


def expire_stale_unconsumed_replies(*, stale_seconds: float = UNCONSUMED_REPLY_STALE_SECONDS) -> int:
    if not _supabase_configured():
        return 0
    client = create_supabase_client(settings)
    response = (
        client.table(COMPANION_REPLIES_TABLE)
        .select("id,created_at,payload")
        .is_("consumed_at", "null")
        .order("created_at", desc=False)
        .limit(100)
        .execute()
    )
    now = datetime.now(timezone.utc)
    expired_ids: list[int] = []
    for row in response.data or []:
        row_id = row.get("id")
        created_at = _parse_created_at(row.get("created_at"))
        if not isinstance(row_id, int) or not created_at:
            continue
        age = (now - created_at).total_seconds()
        if age >= stale_seconds:
            expired_ids.append(row_id)
    if not expired_ids:
        return 0
    client.table(COMPANION_REPLIES_TABLE).update({"consumed_at": utc_now_iso()}).in_("id", expired_ids).execute()
    return len(expired_ids)


def expire_pending_replies_before_player_chat(
    persona: str,
    session_id: str,
    *,
    max_age_seconds: float | None = None,
) -> int:
    if not _supabase_configured():
        return 0
    client = create_supabase_client(settings)
    response = (
        client.table(COMPANION_REPLIES_TABLE)
        .select("id,created_at,payload")
        .eq("persona", persona)
        .is_("consumed_at", "null")
        .order("created_at", desc=False)
        .limit(100)
        .execute()
    )
    now = datetime.now(timezone.utc)
    expired_ids: list[int] = []
    for row in response.data or []:
        row_id = row.get("id")
        if not isinstance(row_id, int):
            continue
        payload = row.get("payload") or {}
        row_session_id = str(payload.get("session_id") or "").strip()
        if session_id and row_session_id and row_session_id != session_id:
            continue
        created_at = _parse_created_at(row.get("created_at"))
        if not created_at:
            continue
        age = (now - created_at).total_seconds()
        if age < -5.0:
            continue
        if max_age_seconds is None or age <= max_age_seconds:
            expired_ids.append(row_id)
    if not expired_ids:
        return 0
    client.table(COMPANION_REPLIES_TABLE).update({"consumed_at": utc_now_iso()}).in_("id", expired_ids).execute()
    return len(expired_ids)


def has_unconsumed_ambient_reply(
    persona: str,
    session_id: str,
    *,
    max_age_seconds: float = UNCONSUMED_REPLY_STALE_SECONDS,
) -> bool:
    if not _supabase_configured():
        return False
    client = create_supabase_client(settings)
    response = (
        client.table(COMPANION_REPLIES_TABLE)
        .select("id,created_at,payload")
        .eq("persona", persona)
        .is_("consumed_at", "null")
        .order("created_at", desc=True)
        .limit(25)
        .execute()
    )
    now = datetime.now(timezone.utc)
    for row in response.data or []:
        payload = row.get("payload") or {}
        if payload.get("trigger") == "ambient_heartbeat" and payload.get("session_id") == session_id:
            created_at = _parse_created_at(row.get("created_at"))
            if created_at and (now - created_at).total_seconds() > max_age_seconds:
                continue
            return True
    return False


def ambient_identity() -> tuple[str, str] | None:
    with world_state_lock:
        persona = world_state.persona.strip()
        if persona.lower() in {"", "unknown character", "system"}:
            return None
        return persona, world_state.session_id


def reply_exists_for_log(log_id: int) -> bool:
    if not _supabase_configured():
        return False
    client = create_supabase_client(settings)
    response = (
        client.table(COMPANION_REPLIES_TABLE)
        .select("id")
        .eq("trigger_log_id", log_id)
        .limit(1)
        .execute()
    )
    return bool(response.data)


def reply_exists_for_environment_alert(alert_id: int) -> bool:
    if not _supabase_configured():
        return False
    client = create_supabase_client(settings)
    response = (
        client.table(COMPANION_REPLIES_TABLE)
        .select("id,payload")
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    )
    for row in response.data or []:
        payload = row.get("payload") or {}
        if payload.get("trigger_environment_alert_id") == alert_id:
            return True
    return False


async def handle_game_log_payload(payload: dict[str, Any], *, use_ollama: bool = False) -> None:
    record = payload.get("record") or payload
    if (record.get("payload") or {}).get("direct_hermes_forwarded"):
        return
    log_id = record.get("id")
    if isinstance(log_id, int) and await asyncio.to_thread(reply_exists_for_log, log_id):
        return
    try:
        event = event_from_game_log(record)
    except Exception:
        return
    await handle_event(event, record_id=log_id, use_ollama=use_ollama)


async def handle_environment_alert_payload(payload: dict[str, Any], *, use_ollama: bool = False) -> None:
    record = payload.get("record") or payload
    if (record.get("payload") or {}).get("direct_hermes_forwarded"):
        return
    alert_id = record.get("id")
    if isinstance(alert_id, int) and await asyncio.to_thread(reply_exists_for_environment_alert, alert_id):
        return
    try:
        event = event_from_environment_alert(record)
    except Exception:
        return
    await handle_event(event, record_id=alert_id, use_ollama=use_ollama)


async def handle_event(event: TelemetryEvent, *, record_id: int | None = None, use_ollama: bool = False) -> None:
    await asyncio.to_thread(handle_event_sync, event, record_id=record_id, use_ollama=use_ollama)


def handle_event_sync(event: TelemetryEvent, *, record_id: int | None = None, use_ollama: bool = False) -> None:
    replies = process_event(event, record_id=record_id, use_ollama=use_ollama)
    if not replies:
        return

    for reply in replies:
        insert_reply(reply)


def process_event(event: TelemetryEvent, *, record_id: int | None = None, use_ollama: bool = False) -> list[CompanionReplyInsert]:
    with world_state_lock:
        if should_ignore_radar_alert(event):
            return []
        previous_map_id = world_state.map_id
        world_state.apply_event(event)
        player_chat_at_generation_start = world_state.last_player_chat_at

        is_direct_player_chat = event.event_type == "player_chat" and event.channel == "party"
        is_party_chat_log = event.event_type == "chat_log" and event.channel == "party"
        is_emergency_alert = event.event_type == "environment_alert" and event.alert_type in EMERGENCY_ALERT_TYPES
        is_emergency_gameplay = event.event_type in {"party_member_down", "party_defeated"}
        is_unknown_quest_change = event.event_type == "active_quest_changed" and not readable_game_text(event.active_quest_name)
        is_unusable_target_change = event.event_type == "target_changed" and not (
            readable_game_text(getattr(event, "agent_name", "")) or has_visible_enemy_context(event)
        )
        is_map_entry = event.event_type in MAP_COMMENT_EVENT_TYPES and bool(event.map_id)
        actual_map_transition = bool(is_map_entry and previous_map_id and previous_map_id != event.map_id)
        if is_unknown_quest_change:
            record_memory_event(event, record_id=record_id)
            return []
        if is_unusable_target_change:
            record_memory_event(event, record_id=record_id)
            return []
        if is_map_entry:
            map_comment_key = (
                event.persona.strip().lower() or "unknown",
                event.session_id or settings.active_session,
                "map-entry",
            )
            has_map_name = bool(map_display_name(event))
            recent_player_chat = (
                world_state.last_player_chat_at > 0
                and time.time() - world_state.last_player_chat_at < MAP_ENTRY_AFTER_PLAYER_CHAT_QUIET_SECONDS
            )
            duplicate_map_entry = last_map_comment_by_session.get(map_comment_key) == event.map_id
            recent_chat_blocks_entry = recent_player_chat and not actual_map_transition
            if recent_chat_blocks_entry or duplicate_map_entry:
                should_speak_now = False
            else:
                should_speak_now = has_map_name and (actual_map_transition or world_state.can_speak(20.0))
                if should_speak_now:
                    last_map_comment_by_session[map_comment_key] = event.map_id
            persona = world_state.persona
            session_id = world_state.session_id
            record_memory_event(event, record_id=record_id)
            if not should_speak_now:
                return []
        else:
            map_comment_key = None
        if event.event_type == "environment_alert" and event.alert_type == "under_attack":
            required_cooldown = 3.0
        elif event.event_type == "environment_alert" and event.alert_type == "status_effect":
            required_cooldown = 5.0
        elif event.event_type == "environment_alert" and event.alert_type == "combat_over":
            required_cooldown = 7.0
        elif is_emergency_alert or is_emergency_gameplay:
            required_cooldown = 5.0
        elif event.event_type == "target_changed":
            required_cooldown = 6.0
        elif is_npc_dialogue_event(event):
            required_cooldown = 14.0
        elif is_ambient_snapshot_event(event):
            required_cooldown = AMBIENT_QUIP_MIN_SECONDS
        else:
            required_cooldown = settings.hermes_min_speak_seconds
        if not is_map_entry and not (is_direct_player_chat or is_party_chat_log) and not world_state.can_speak(required_cooldown):
            should_speak_now = False
        elif not is_map_entry:
            should_speak_now = True

        persona = world_state.persona
        session_id = world_state.session_id
    if not is_map_entry:
        record_memory_event(event, record_id=record_id)
    if not should_speak_now:
        return []
    if is_direct_player_chat:
        expired = expire_pending_replies_before_player_chat(persona, session_id)
        if expired:
            print(f"Hermes expired {expired} pending replies before player chat.", flush=True)
    if use_ollama and should_use_fast_fallback_before_ollama(event):
        decision = fallback_rule_decision(event)
    elif use_ollama and should_use_ollama_for_event(event):
        try:
            decision = decide_with_ollama(event, record_id=record_id)
        except Exception as exc:
            detail = str(exc).strip()
            suffix = f": {detail}" if detail else ""
            print(
                f"Ollama decision failed for {event_debug_label(event, record_id=record_id)}; "
                f"using fallback rules ({type(exc).__name__}{suffix}).",
                flush=True,
            )
            decision = fallback_rule_decision(event)
    else:
        decision = fallback_rule_decision(event)
    if event.event_type in MAP_COMMENT_EVENT_TYPES or is_ambient_snapshot_event(event):
        with world_state_lock:
            if world_state.last_player_chat_at > player_chat_at_generation_start:
                return []
    replies = replies_from_decision(
        decision,
        persona=persona,
        session_id=session_id,
        trigger_log_id=record_id if event.event_type != "environment_alert" else None,
    )
    if is_direct_player_chat and any(is_duplicate_direct_reply(reply.message) for reply in replies):
        recovery = HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="NORMAL",
            response=clamp_gw_chat_line(duplicate_recovery_reply()),
        )
        replies = replies_from_decision(
            recovery,
            persona=persona,
            session_id=session_id,
            trigger_log_id=record_id,
        )
    if not replies:
        return []
    if event.event_type == "environment_alert" and isinstance(record_id, int):
        for reply in replies:
            reply.metadata["trigger_environment_alert_id"] = record_id
    for reply in replies:
        reply.metadata["trigger_event_type"] = event.event_type
        reply.metadata["trigger_channel"] = event.channel

    with world_state_lock:
        for reply in replies:
            recent_reply_texts.append(reply.message)
        world_state.mark_spoken()
    return replies


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "gwplaymate-hermes",
        "mode": "ollama" if settings.hermes_use_ollama else "fallback",
        "model": settings.ollama_model,
        "supabase_configured": _supabase_configured(),
        "realtime_enabled": settings.hermes_enable_realtime,
    }


@app.post("/v1/hermes/events", response_model=HermesEventResponse)
def post_direct_event(event: TelemetryEvent) -> HermesEventResponse:
    replies = process_event(event, use_ollama=settings.hermes_use_ollama)
    if not replies:
        return HermesEventResponse(replies=[])

    audit_error = None
    delivered_replies = replies
    if settings.hermes_audit_replies:
        delivered_replies = []
        try:
            for reply in replies:
                inserted = insert_reply(reply, consumed=True)
                if inserted:
                    delivered_replies.append(inserted)
        except Exception as exc:
            audit_error = str(exc)
    return HermesEventResponse(replies=[reply.message for reply in delivered_replies], audit_error=audit_error)


async def subscribe_to_game_logs() -> None:
    require_supabase_settings(settings)

    client = await acreate_client(settings.supabase_url, settings.supabase_service_key)
    channel = client.channel("gwplaymate-game-logs")
    channel.on_postgres_changes(
        "INSERT",
        callback=lambda payload: asyncio.create_task(
            handle_game_log_payload(payload, use_ollama=settings.hermes_use_ollama)
        ),
        table=GAME_LOGS_TABLE,
        schema="public",
    )
    channel.on_postgres_changes(
        "INSERT",
        callback=lambda payload: asyncio.create_task(
            handle_environment_alert_payload(payload, use_ollama=settings.hermes_use_ollama)
        ),
        table=ENVIRONMENT_ALERTS_TABLE,
        schema="public",
    )
    await channel.subscribe()


def poll_state_path() -> Path:
    if settings.hermes_poll_state_path:
        return Path(settings.hermes_poll_state_path).expanduser()
    return DEFAULT_POLL_STATE_PATH


def load_poll_watermarks() -> dict[str, int]:
    path = poll_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"game_logs": 0, "environment_alerts": 0}
    return {
        "game_logs": int(payload.get("game_logs") or 0),
        "environment_alerts": int(payload.get("environment_alerts") or 0),
    }


def save_poll_watermarks(watermarks: dict[str, int]) -> None:
    path = poll_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(watermarks, indent=2, sort_keys=True), encoding="utf-8")


def fetch_records_after_id(client: Any, table_name: str, last_id: int) -> list[dict[str, Any]]:
    response = (
        client.table(table_name)
        .select("*")
        .gt("id", last_id)
        .order("id", desc=False)
        .limit(settings.hermes_poll_batch_size)
        .execute()
    )
    return list(response.data or [])


async def poll_supabase_events() -> None:
    require_supabase_settings(settings)
    client = create_supabase_client(settings)
    watermarks = load_poll_watermarks()
    active_until = 0.0
    print("GWPlaymate Hermes polling Supabase with stored watermarks.", flush=True)
    await asyncio.sleep(0)

    while True:
        processed = 0
        try:
            game_logs = await asyncio.to_thread(fetch_records_after_id, client, GAME_LOGS_TABLE, watermarks["game_logs"])
            for record in game_logs:
                record_id = record.get("id")
                if isinstance(record_id, int):
                    watermarks["game_logs"] = max(watermarks["game_logs"], record_id)
                if not is_stale_polled_record(record):
                    processed += 1
                    await handle_game_log_payload({"record": record}, use_ollama=settings.hermes_use_ollama)

            alerts = await asyncio.to_thread(
                fetch_records_after_id,
                client,
                ENVIRONMENT_ALERTS_TABLE,
                watermarks["environment_alerts"],
            )
            for record in alerts:
                record_id = record.get("id")
                if isinstance(record_id, int):
                    watermarks["environment_alerts"] = max(watermarks["environment_alerts"], record_id)
                if is_stale_polled_record(record):
                    continue
                processed += 1
                await handle_environment_alert_payload({"record": record}, use_ollama=settings.hermes_use_ollama)

            if game_logs or alerts:
                await asyncio.to_thread(save_poll_watermarks, watermarks)
            if processed:
                active_until = time.monotonic() + settings.hermes_poll_active_window_seconds
        except Exception as exc:
            print(f"GWPlaymate Hermes Supabase poll error: {type(exc).__name__}.", flush=True)

        delay = settings.hermes_poll_active_seconds if time.monotonic() < active_until else settings.hermes_poll_idle_seconds
        await asyncio.sleep(delay)


async def ambient_heartbeat_loop() -> None:
    print("GWPlaymate Hermes ambient heartbeat enabled.", flush=True)
    while True:
        try:
            expired = await asyncio.to_thread(expire_stale_unconsumed_replies)
            if expired:
                print(f"Hermes expired {expired} stale unconsumed companion replies.", flush=True)
            identity = ambient_identity() if _supabase_configured() else None
            if identity:
                pending = await asyncio.to_thread(has_unconsumed_ambient_reply, identity[0], identity[1])
                reply = None if pending else ambient_heartbeat_reply(use_ollama=settings.hermes_use_ollama)
                if reply:
                    inserted = await asyncio.to_thread(insert_reply, reply)
                    if inserted:
                        print(f"Hermes ambient quip inserted for {inserted.persona}: {inserted.message}", flush=True)
        except Exception as exc:
            print(f"GWPlaymate Hermes ambient heartbeat error: {type(exc).__name__}.", flush=True)
        await asyncio.sleep(AMBIENT_HEARTBEAT_POLL_SECONDS)


async def main_async() -> None:
    if _supabase_configured():
        require_supabase_settings(settings)
        if settings.hermes_enable_realtime:
            await subscribe_to_game_logs()
        asyncio.create_task(poll_supabase_events())
        asyncio.create_task(ambient_heartbeat_loop())
    if settings.hermes_use_ollama:
        asyncio.create_task(ollama_keepalive_loop())
    mode = "Ollama" if settings.hermes_use_ollama else "fallback rules"
    print(f"GWPlaymate companion daemon listening on {settings.hermes_host}:{settings.hermes_port} ({mode}).")
    if settings.hermes_enable_realtime:
        print("Supabase Realtime subscription is enabled for audit/backfill events.")
    elif _supabase_configured():
        print("Supabase polling is enabled; Realtime subscriptions are disabled for free-tier safety.")
    config = uvicorn.Config(app, host=settings.hermes_host, port=settings.hermes_port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    asyncio.run(main_async())


atexit.register(flush_all_memory_buffers)


if __name__ == "__main__":
    main()
