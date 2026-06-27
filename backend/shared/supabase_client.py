from __future__ import annotations

from supabase import Client, create_client

from backend.shared.config import BackendSettings


def require_supabase_settings(settings: BackendSettings, *, prefer_publishable: bool = False) -> None:
    if not settings.supabase_url:
        raise RuntimeError("SUPABASE_URL is required")
    key = settings.supabase_publishable_key if prefer_publishable else settings.supabase_service_key
    if not key:
        name = "SUPABASE_PUBLISHABLE_KEY" if prefer_publishable else "SUPABASE_SERVICE_KEY"
        raise RuntimeError(f"{name} is required")


def create_supabase_client(settings: BackendSettings, *, prefer_publishable: bool = False) -> Client:
    require_supabase_settings(settings, prefer_publishable=prefer_publishable)
    key = settings.supabase_publishable_key if prefer_publishable else settings.supabase_service_key
    return create_client(settings.supabase_url, key)
