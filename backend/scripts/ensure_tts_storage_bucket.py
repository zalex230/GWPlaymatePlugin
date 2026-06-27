from __future__ import annotations

from backend.shared.config import load_settings
from backend.shared.supabase_client import create_supabase_client, require_supabase_settings


def main() -> None:
    settings = load_settings()
    require_supabase_settings(settings)

    bucket_name = settings.hermes_tts_storage_bucket
    client = create_supabase_client(settings)

    try:
        client.storage.get_bucket(bucket_name)
        print(f"Supabase Storage bucket already exists: {bucket_name}")
        return
    except Exception as exc:
        message = str(exc).lower()
        if "not found" not in message and "404" not in message:
            raise

    client.storage.create_bucket(
        bucket_name,
        options={
            "public": False,
            "file_size_limit": 5_242_880,
            "allowed_mime_types": ["audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg"],
        },
    )
    print(f"Created private Supabase Storage bucket: {bucket_name}")


if __name__ == "__main__":
    main()
