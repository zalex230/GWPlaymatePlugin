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


def _insert_event(event: TelemetryEvent) -> dict[str, Any]:
    if event.channel in NOISY_CHANNELS:
        return {"accepted": False, "reason": "noisy_channel"}
    if event.event_type in SUPPRESSED_EVENT_TYPES:
        return {"accepted": False, "reason": "suppressed_event_type"}
    if not throttle.should_accept(event):
        return {"accepted": False, "reason": "throttled"}

    client = _client()
    if event.event_type in ENVIRONMENT_EVENT_TYPES:
        client.table(ENVIRONMENT_ALERTS_TABLE).insert(event.to_environment_alert_insert()).execute()
    else:
        client.table(GAME_LOGS_TABLE).insert(event.to_game_log_insert()).execute()
    return {"accepted": True}


def _strip_invalid_json_control_chars(text: str) -> str:
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 0x20)


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
        if rows:
            consumed_at = datetime.now(timezone.utc).isoformat()
            ids = [row.id for row in rows]
            client.table(COMPANION_REPLIES_TABLE).update({"consumed_at": consumed_at}).in_("id", ids).execute()
        return RepliesResponse(replies=[row.message for row in rows])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def main() -> None:
    require_supabase_settings(settings)
    uvicorn.run("backend.windows_bridge.app:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
