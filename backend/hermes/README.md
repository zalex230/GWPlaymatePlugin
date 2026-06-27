# GWPlaymate Hermes daemon

Hermes is the Mac-side companion brain for GWPlaymate.

Current pipeline:

```text
GWToolbox Playmate plugin
  -> Windows bridge
  -> Supabase game_logs / environment_alerts
  -> Hermes daemon
  -> Ollama
  -> Supabase companion_replies
  -> Windows bridge / GWToolbox chat
```

The daemon is responsible for:

- reading game telemetry from Supabase Realtime and polling fallback;
- generating short in-character companion replies through Ollama;
- writing replies to `companion_replies`;
- writing compact durable memories to `memories`;
- retrieving recent memories as prompt context;
- keeping personality/persona notes in `backend/hermes/personas/`.

Run locally:

```bash
python -m backend.hermes.daemon
```

Useful environment variables live in `backend/.env`:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `HERMES_USE_OLLAMA`
- `OLLAMA_HOST`
- `OLLAMA_MODEL`
- `OLLAMA_NUM_CTX`
- `OLLAMA_NUM_PREDICT`
- `HERMES_TTS_PROVIDER`
- `KOKORO_TTS_URL`
- `KOKORO_TTS_VOICE`
- `HERMES_TTS_STORAGE_BUCKET`
- `HERMES_TTS_SIGNED_URL_SECONDS`

Do not commit `backend/.env`; it contains service-role credentials.

## TTS audio over Supabase

Set `HERMES_TTS_PROVIDER=kokoro` to have Hermes call a local Kokoro-compatible endpoint, upload the
generated audio to private Supabase Storage, and attach a signed URL to `companion_replies.payload`.
The Windows bridge returns that URL to the plugin in `reply_items`; the plugin downloads and plays it
locally. Leave `HERMES_TTS_PROVIDER=none` to keep text-only replies.

On macOS, install and start a local Kokoro-FastAPI service with:

```bash
bash backend/scripts/install-kokoro-launchagent.sh
```

The script clones Kokoro-FastAPI into `~/Documents/Kokoro-FastAPI` by default, installs its CPU
dependencies with `uv`, downloads the model files, and writes a user LaunchAgent named
`com.gwplaymate.kokoro-fastapi`. Override `KOKORO_ROOT`, `HOST`, or `PORT` before running the script
if a machine needs different paths or binding.

Create the private Supabase Storage bucket if `backend/supabase/setup.sql` has not already been run:

```bash
python -m backend.scripts.ensure_tts_storage_bucket
```

Hermes writes text-only replies if Kokoro is offline or if Storage upload/signing fails. Check those two
services before debugging the Windows audio fallback.

Health check:

```bash
curl http://127.0.0.1:8797/health
curl http://127.0.0.1:8880/health
```

Tests:

```bash
python -m unittest discover -s backend/tests -p "test_*.py"
```
