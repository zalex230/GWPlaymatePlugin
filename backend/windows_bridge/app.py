from __future__ import annotations

import json
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from backend.shared.config import load_settings
from backend.shared.constants import (
    APPROVED_ENVIRONMENT_ALERT_TYPES,
    COMPANION_REPLIES_TABLE,
    ENVIRONMENT_ALERTS_TABLE,
    ENVIRONMENT_EVENT_TYPES,
    GAME_LOGS_TABLE,
    NOISY_CHANNELS,
    SUPPRESSED_EVENT_TYPES,
)
from backend.shared.models import CompanionReplyRow, RepliesResponse, TelemetryEvent
from backend.shared.supabase_client import create_supabase_client, require_supabase_settings
from backend.shared.throttle import EventThrottle


settings = load_settings()
app = FastAPI(title="GWPlaymate Windows Bridge", version="0.1.0")
throttle = EventThrottle(settings.snapshot_min_interval_seconds)


def _client():
    return create_supabase_client(settings)


def _consume_pending_replies_before_player_chat(event: TelemetryEvent, client: Any) -> int:
    if event.event_type != "player_chat" or event.channel != "party":
        return 0
    response = (
        client.table(COMPANION_REPLIES_TABLE)
        .select("id,payload")
        .eq("persona", event.persona)
        .is_("consumed_at", "null")
        .order("created_at", desc=False)
        .limit(100)
        .execute()
    )
    pending_ids: list[int] = []
    for row in response.data or []:
        row_id = row.get("id")
        if not isinstance(row_id, int):
            continue
        payload = row.get("payload") or {}
        row_session_id = str(payload.get("session_id") or "").strip()
        if event.session_id and row_session_id and row_session_id != event.session_id:
            continue
        pending_ids.append(row_id)
    if not pending_ids:
        return 0
    client.table(COMPANION_REPLIES_TABLE).update({"consumed_at": datetime.now(timezone.utc).isoformat()}).in_(
        "id",
        pending_ids,
    ).execute()
    return len(pending_ids)


def _insert_event(event: TelemetryEvent) -> dict[str, Any]:
    if event.channel in NOISY_CHANNELS:
        return {"accepted": False, "reason": "noisy_channel"}
    if event.event_type in SUPPRESSED_EVENT_TYPES:
        return {"accepted": False, "reason": "suppressed_event_type"}
    if event.event_type in ENVIRONMENT_EVENT_TYPES and event.alert_type not in APPROVED_ENVIRONMENT_ALERT_TYPES:
        return {"accepted": False, "reason": "unsupported_environment_alert"}
    if not throttle.should_accept(event):
        return {"accepted": False, "reason": "throttled"}

    client = _client()
    try:
        consumed = _consume_pending_replies_before_player_chat(event, client)
        if consumed:
            print(f"Windows bridge consumed {consumed} pending replies before player chat.", flush=True)
    except Exception as exc:
        print(f"Windows bridge pending reply cleanup skipped: {type(exc).__name__}.", flush=True)
    if event.event_type in ENVIRONMENT_EVENT_TYPES:
        client.table(ENVIRONMENT_ALERTS_TABLE).insert(event.to_environment_alert_insert()).execute()
    else:
        client.table(GAME_LOGS_TABLE).insert(event.to_game_log_insert()).execute()
    return {"accepted": True}


def _strip_invalid_json_control_chars(text: str) -> str:
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 0x20)


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_fresh_reply(row: CompanionReplyRow, now: datetime) -> bool:
    created_at = _parse_created_at(row.created_at)
    if created_at is None:
        return True
    age = (now - created_at).total_seconds()
    return -5.0 <= age <= settings.reply_max_age_seconds


def _latest_player_chat_at(client: Any, *, session_id: str | None = None) -> datetime | None:
    response = (
        client.table(GAME_LOGS_TABLE)
        .select("created_at,channel,payload")
        .eq("channel", "party")
        .order("created_at", desc=True)
        .limit(25)
        .execute()
    )
    for row in response.data or []:
        payload = row.get("payload") or {}
        if payload.get("event_type") != "player_chat":
            continue
        row_session_id = str(payload.get("session_id") or "").strip()
        if session_id and row_session_id and row_session_id != session_id:
            continue
        created_at = _parse_created_at(row.get("created_at"))
        if created_at:
            return created_at
    return None


def _is_interrupted_by_newer_player_chat(row: CompanionReplyRow, latest_player_chat_at: datetime | None) -> bool:
    if latest_player_chat_at is None:
        return False
    created_at = _parse_created_at(row.created_at)
    if created_at is None:
        return False
    return created_at <= latest_player_chat_at


async def _event_from_request(request: Request) -> TelemetryEvent:
    raw = await request.body()
    text = raw.decode("utf-8", errors="replace")
    text = _strip_invalid_json_control_chars(text)
    try:
        payload = json.loads(text)
    except JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid telemetry JSON: {exc.msg}") from exc
    try:
        return TelemetryEvent.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "gwplaymate-windows-bridge",
        "supabase_configured": bool(settings.supabase_url and settings.supabase_service_key),
    }


@app.post("/v1/playmate/events")
async def post_event(request: Request) -> dict[str, Any]:
    try:
        event = await _event_from_request(request)
        return _insert_event(event)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/playmate/replies", response_model=RepliesResponse)
def get_replies(persona: str | None = None, session_id: str | None = None, limit: int | None = None) -> RepliesResponse:
    client = _client()
    query = (
        client.table(COMPANION_REPLIES_TABLE)
        .select("*")
        .is_("consumed_at", "null")
        .order("created_at", desc=False)
        .limit(limit or settings.reply_limit)
    )
    if persona:
        query = query.eq("persona", persona)

    try:
        response = query.execute()
        rows = [CompanionReplyRow.model_validate(row) for row in response.data or []]
        if session_id:
            rows = [row for row in rows if row.payload_session_id() == session_id]
        now = datetime.now(timezone.utc)
        latest_player_chat_at = _latest_player_chat_at(client, session_id=session_id)
        fresh_rows = [
            row
            for row in rows
            if _is_fresh_reply(row, now) and not _is_interrupted_by_newer_player_chat(row, latest_player_chat_at)
        ]
        fresh_ids = {row.id for row in fresh_rows}
        stale_rows = [row for row in rows if row.id not in fresh_ids]
        if rows:
            consumed_at = datetime.now(timezone.utc).isoformat()
            ids = [row.id for row in rows]
            client.table(COMPANION_REPLIES_TABLE).update({"consumed_at": consumed_at}).in_("id", ids).execute()
        if stale_rows:
            print(f"Windows bridge dropped {len(stale_rows)} stale companion replies.", flush=True)
        reply_items = [row.to_reply_item() for row in fresh_rows]
        return RepliesResponse(replies=[item.message for item in reply_items], reply_items=reply_items)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def main() -> None:
    require_supabase_settings(settings)
    uvicorn.run("backend.windows_bridge.app:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
