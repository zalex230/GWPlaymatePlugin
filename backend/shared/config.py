from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_PATH)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    return float(value)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BackendSettings:
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_key: str = os.getenv("SUPABASE_SERVICE_KEY", "")
    supabase_publishable_key: str = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
    host: str = os.getenv("PLAYMATE_HOST", "127.0.0.1")
    port: int = _int_env("PLAYMATE_PORT", 8787)
    active_session: str = os.getenv("PLAYMATE_ACTIVE_SESSION", "local-playtest")
    reply_limit: int = _int_env("PLAYMATE_REPLY_LIMIT", 8)
    snapshot_min_interval_seconds: float = _float_env("PLAYMATE_SNAPSHOT_MIN_INTERVAL_SECONDS", 8.0)
    hermes_direct_url: str = os.getenv("HERMES_DIRECT_URL", "")
    hermes_direct_timeout_seconds: float = _float_env("HERMES_DIRECT_TIMEOUT_SECONDS", 90.0)
    hermes_host: str = os.getenv("HERMES_HOST", "127.0.0.1")
    hermes_port: int = _int_env("HERMES_PORT", 8797)
    hermes_enable_realtime: bool = _bool_env("HERMES_ENABLE_REALTIME", True)
    hermes_audit_replies: bool = _bool_env("HERMES_AUDIT_REPLIES", True)
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "gemma3:12b")
    ollama_num_ctx: int = _int_env("OLLAMA_NUM_CTX", 8192)
    ollama_num_predict: int = _int_env("OLLAMA_NUM_PREDICT", 48)
    hermes_use_ollama: bool = _bool_env("HERMES_USE_OLLAMA", False)
    hermes_min_speak_seconds: float = _float_env("HERMES_MIN_SPEAK_SECONDS", 20.0)
    recent_chat_limit: int = _int_env("HERMES_RECENT_CHAT_LIMIT", 10)
    recent_alert_limit: int = _int_env("HERMES_RECENT_ALERT_LIMIT", 8)


def load_settings() -> BackendSettings:
    return BackendSettings()
