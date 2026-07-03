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
    reply_max_age_seconds: float = _float_env("PLAYMATE_REPLY_MAX_AGE_SECONDS", 60.0)
    snapshot_min_interval_seconds: float = _float_env("PLAYMATE_SNAPSHOT_MIN_INTERVAL_SECONDS", 30.0)
    hermes_direct_url: str = os.getenv("HERMES_DIRECT_URL", "")
    hermes_direct_timeout_seconds: float = _float_env("HERMES_DIRECT_TIMEOUT_SECONDS", 90.0)
    hermes_host: str = os.getenv("HERMES_HOST", "127.0.0.1")
    hermes_port: int = _int_env("HERMES_PORT", 8797)
    hermes_enable_realtime: bool = _bool_env("HERMES_ENABLE_REALTIME", False)
    hermes_audit_replies: bool = _bool_env("HERMES_AUDIT_REPLIES", True)
    hermes_poll_idle_seconds: float = _float_env("HERMES_POLL_IDLE_SECONDS", 15.0)
    hermes_poll_active_seconds: float = _float_env("HERMES_POLL_ACTIVE_SECONDS", 3.0)
    hermes_poll_active_window_seconds: float = _float_env("HERMES_POLL_ACTIVE_WINDOW_SECONDS", 75.0)
    hermes_poll_batch_size: int = _int_env("HERMES_POLL_BATCH_SIZE", 50)
    hermes_poll_state_path: str = os.getenv("HERMES_POLL_STATE_PATH", "")
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "hermes-qwen35-4b:latest")
    ollama_num_ctx: int = _int_env("OLLAMA_NUM_CTX", 8192)
    ollama_num_predict: int = _int_env("OLLAMA_NUM_PREDICT", 40)
    ollama_timeout_seconds: float = _float_env("OLLAMA_TIMEOUT_SECONDS", 120.0)
    hermes_player_chat_ollama_timeout_seconds: float = _float_env("HERMES_PLAYER_CHAT_OLLAMA_TIMEOUT_SECONDS", 15.0)
    hermes_use_ollama: bool = _bool_env("HERMES_USE_OLLAMA", False)
    hermes_min_speak_seconds: float = _float_env("HERMES_MIN_SPEAK_SECONDS", 20.0)
    recent_chat_limit: int = _int_env("HERMES_RECENT_CHAT_LIMIT", 10)
    recent_alert_limit: int = _int_env("HERMES_RECENT_ALERT_LIMIT", 8)
    hermes_tts_provider: str = os.getenv("HERMES_TTS_PROVIDER", "none").strip().lower()
    hermes_tts_storage_bucket: str = os.getenv("HERMES_TTS_STORAGE_BUCKET", "playmate-tts")
    hermes_tts_signed_url_seconds: int = _int_env("HERMES_TTS_SIGNED_URL_SECONDS", 600)
    kokoro_tts_url: str = os.getenv("KOKORO_TTS_URL", "http://127.0.0.1:8880/v1/audio/speech")
    kokoro_tts_model: str = os.getenv("KOKORO_TTS_MODEL", "kokoro")
    kokoro_tts_voice: str = os.getenv("KOKORO_TTS_VOICE", "af_bella")
    kokoro_tts_format: str = os.getenv("KOKORO_TTS_FORMAT", "mp3")
    kokoro_tts_timeout_seconds: float = _float_env("KOKORO_TTS_TIMEOUT_SECONDS", 20.0)
    chatterbox_tts_url: str = os.getenv("CHATTERBOX_TTS_URL", "http://127.0.0.1:4123/v1/audio/speech")
    chatterbox_tts_voice_sample: str = os.getenv("CHATTERBOX_TTS_VOICE_SAMPLE", "")
    chatterbox_tts_format: str = os.getenv("CHATTERBOX_TTS_FORMAT", "wav")
    chatterbox_tts_timeout_seconds: float = _float_env("CHATTERBOX_TTS_TIMEOUT_SECONDS", 90.0)
    chatterbox_tts_exaggeration: float = _float_env("CHATTERBOX_TTS_EXAGGERATION", 0.7)
    chatterbox_tts_temperature: float = _float_env("CHATTERBOX_TTS_TEMPERATURE", 0.8)


def load_settings() -> BackendSettings:
    return BackendSettings()
