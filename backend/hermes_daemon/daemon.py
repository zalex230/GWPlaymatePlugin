from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from supabase import acreate_client

from backend.shared.config import load_settings
from backend.shared.constants import COMPANION_REPLIES_TABLE, ENVIRONMENT_ALERTS_TABLE, GAMEPLAY_EVENT_TYPES, GAME_LOGS_TABLE
from backend.shared.models import CompanionReplyInsert, HermesDecision, TelemetryEvent
from backend.shared.state import LiveWorldState
from backend.shared.supabase_client import create_supabase_client, require_supabase_settings


settings = load_settings()
world_state = LiveWorldState(
    recent_chat_limit=settings.recent_chat_limit,
    recent_alert_limit=settings.recent_alert_limit,
    session_id=settings.active_session,
)


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
        map_name=metadata.get("map_name", ""),
        instance_type=record.get("instance_type") or metadata.get("instance_type", 0),
        district=record.get("district") or metadata.get("district", 0),
        instance_time=record.get("instance_time") or metadata.get("instance_time", 0),
        active_quest_id=record.get("active_quest_id") or metadata.get("active_quest_id", 0),
        quest_count=record.get("quest_count") or metadata.get("quest_count", 0),
        active_quest_name=record.get("active_quest_name") or metadata.get("active_quest_name", ""),
        active_quest_objectives=record.get("active_quest_objectives") or metadata.get("active_quest_objectives", ""),
        player_x=metadata.get("player_x", record.get("player_x") or 0),
        player_y=metadata.get("player_y", record.get("player_y") or 0),
        player_hp=metadata.get("player_hp", 0),
        hostile_count=metadata.get("hostile_count", 0),
        close_hostile_count=metadata.get("close_hostile_count", 0),
        dead_hostile_count=metadata.get("dead_hostile_count", 0),
        closest_hostile_agent_id=metadata.get("closest_hostile_agent_id", record.get("agent_id") or 0),
        closest_hostile_distance=metadata.get("closest_hostile_distance", record.get("distance") or 0),
        alert_type=metadata.get("alert_type", record.get("alert_type") or ""),
        severity=metadata.get("severity", record.get("severity") or "NORMAL"),
        agent_id=metadata.get("agent_id", record.get("agent_id") or 0),
        agent_name=metadata.get("agent_name", ""),
        objective_id=metadata.get("objective_id", 0),
        objective_name=metadata.get("objective_name", ""),
        progress_current=metadata.get("progress_current", 0),
        progress_total=metadata.get("progress_total", 0),
        foes_killed=metadata.get("foes_killed", 0),
        foes_remaining=metadata.get("foes_remaining", 0),
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
        map_name=metadata.get("map_name", ""),
        instance_type=metadata.get("instance_type", 0),
        district=metadata.get("district", 0),
        instance_time=metadata.get("instance_time", 0),
        active_quest_id=metadata.get("active_quest_id", 0),
        quest_count=metadata.get("quest_count", 0),
        active_quest_name=metadata.get("active_quest_name", ""),
        active_quest_objectives=metadata.get("active_quest_objectives", ""),
        player_x=metadata.get("player_x", record.get("player_x") or 0),
        player_y=metadata.get("player_y", record.get("player_y") or 0),
        player_hp=metadata.get("player_hp", 0),
        hostile_count=metadata.get("hostile_count", 0),
        close_hostile_count=metadata.get("close_hostile_count", 0),
        dead_hostile_count=metadata.get("dead_hostile_count", 0),
        closest_hostile_agent_id=metadata.get("closest_hostile_agent_id", record.get("agent_id") or 0),
        closest_hostile_distance=metadata.get("closest_hostile_distance", record.get("distance") or 0),
        alert_type=metadata.get("alert_type", record.get("alert_type") or ""),
        severity=metadata.get("severity", record.get("severity") or "NORMAL"),
        agent_id=metadata.get("agent_id", record.get("agent_id") or 0),
        agent_name=metadata.get("agent_name", ""),
        objective_id=metadata.get("objective_id", 0),
        objective_name=metadata.get("objective_name", ""),
        progress_current=metadata.get("progress_current", 0),
        progress_total=metadata.get("progress_total", 0),
        foes_killed=metadata.get("foes_killed", 0),
        foes_remaining=metadata.get("foes_remaining", 0),
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


def build_decision_prompt(event: TelemetryEvent) -> str:
    return (
        "You are the Playmate companion speech gate for Guild Wars 1.\n"
        "Decide whether the companion should speak in-game now.\n"
        "Return exactly one JSON object with keys: should_speak, channel_override, urgency, response.\n"
        "Valid channel_override values: CHANNEL_PARTY, CHANNEL_LOCAL, CHANNEL_SYSTEM.\n"
        "Use CHANNEL_PARTY only for direct player interaction or high urgency danger.\n"
        "Stay concise and in-character. If unsure, set should_speak false.\n\n"
        f"Incoming event: {event.model_dump_json()}\n\n"
        f"Live world state:\n{world_state.prompt_context()}"
    )


def decide_with_ollama(event: TelemetryEvent) -> HermesDecision:
    import ollama

    prompt = build_decision_prompt(event)
    response = ollama.generate(
        model=settings.ollama_model,
        prompt=prompt,
        options={"temperature": 0.3},
    )
    raw = response.get("response", "{}")
    return HermesDecision.model_validate(extract_json_object(raw))


def fallback_rule_decision(event: TelemetryEvent) -> HermesDecision:
    if event.event_type == "player_chat" and event.channel == "party":
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="NORMAL",
            response="I'm here. I caught that.",
        )
    if event.event_type == "environment_alert":
        if event.alert_type == "combat_over":
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="LOW",
                response="Looks clear for the moment.",
            )
        if event.alert_type == "danger_spike":
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="HIGH",
                response=f"Careful. {event.close_hostile_count} enemies are close.",
            )
        if event.alert_type == "under_attack":
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="HIGH",
                response=f"Taking hits. Health is around {event.player_hp:.0%}. Cover me.",
            )
        if event.alert_type == "combat_started":
            return HermesDecision(
                should_speak=True,
                channel_override="CHANNEL_PARTY",
                urgency="HIGH",
                response="Contact. Fight's on.",
            )
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="HIGH",
            response="Careful. Something nearby looks dangerous.",
        )
    if event.event_type == "party_member_down":
        name = f" {event.agent_name}" if event.agent_name else ""
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="HIGH",
            response=f"{name.strip() or 'Someone'} is down. Pull back and stabilize.",
        )
    if event.event_type == "party_defeated":
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="HIGH",
            response="We wiped. Let's reset, rebuff, and take the next pull slower.",
        )
    if event.event_type == "mission_objective_completed":
        objective = f": {event.objective_name}" if event.objective_name else ""
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="NORMAL",
            response=f"Objective complete{objective}. Good push.",
        )
    if event.event_type == "vanquish_complete":
        return HermesDecision(
            should_speak=True,
            channel_override="CHANNEL_PARTY",
            urgency="HIGH",
            response="Vanquish complete. Nice clear.",
        )
    if event.event_type in GAMEPLAY_EVENT_TYPES:
        return HermesDecision(should_speak=False)
    return HermesDecision(should_speak=False)


def insert_reply(reply: CompanionReplyInsert) -> None:
    client = create_supabase_client(settings)
    client.table(COMPANION_REPLIES_TABLE).insert(reply.to_supabase_insert()).execute()


async def handle_game_log_payload(payload: dict[str, Any], *, use_ollama: bool = False) -> None:
    record = payload.get("record") or payload
    event = event_from_game_log(record)
    await handle_event(event, record_id=record.get("id"), use_ollama=use_ollama)


async def handle_environment_alert_payload(payload: dict[str, Any], *, use_ollama: bool = False) -> None:
    record = payload.get("record") or payload
    event = event_from_environment_alert(record)
    await handle_event(event, record_id=record.get("id"), use_ollama=use_ollama)


async def handle_event(event: TelemetryEvent, *, record_id: int | None = None, use_ollama: bool = False) -> None:
    world_state.apply_event(event)

    if not world_state.can_speak(settings.hermes_min_speak_seconds):
        return

    decision = decide_with_ollama(event) if use_ollama else fallback_rule_decision(event)
    reply = decision.to_reply(
        persona=world_state.persona,
        session_id=world_state.session_id,
        trigger_log_id=record_id if event.event_type != "environment_alert" else None,
    )
    if not reply:
        return

    insert_reply(reply)
    world_state.mark_spoken()


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


async def main_async() -> None:
    require_supabase_settings(settings)
    await subscribe_to_game_logs()
    mode = "Ollama" if settings.hermes_use_ollama else "fallback rules"
    print(f"GWPlaymate Hermes daemon listening for Supabase game_logs inserts ({mode}).")
    while True:
        await asyncio.sleep(1)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
