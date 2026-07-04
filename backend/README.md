# GWPlaymate Backend

This backend is the first bridge between the GWToolbox++ Playmate plugin, Supabase, and a Mac Mini running Hermes/Ollama.

```text
GWPlaymate.dll -> Windows localhost bridge -> Supabase -> Hermes daemon -> Ollama
```

The C++ plugin still captures local JSONL logs. Cloud credentials live only in these Python services.
When Hermes generates TTS audio, it uploads the file to private Supabase Storage and stores a short-lived
signed URL on the companion reply. The plugin downloads that URL and plays it locally on the Gaming PC.

## Layout

- `shared/` contains Pydantic models, event names, throttling helpers, and RAM world-state types.
- `windows_bridge/` exposes the plugin-compatible HTTP API on `127.0.0.1:8787`.
- `hermes_daemon/` listens to Supabase Postgres Changes and writes companion replies.
- `supabase/` contains SQL setup/compatibility checks for existing GWPlaymate tables.
- `tests/` covers payload validation, throttling, state updates, and Hermes decision parsing.

## Setup

Create a virtual environment and install pinned dependencies:

```powershell
cd C:\dev\GWPlaymate
python -m venv .venv-playmate
.\.venv-playmate\Scripts\Activate.ps1
pip install -r backend\requirements.txt
```

Copy the template and fill in your Supabase values:

```powershell
Copy-Item backend\.env.example backend\.env
```

Use your own Supabase project URL and keys in `backend/.env`. For example,
`SUPABASE_URL=https://your-project-ref.supabase.co`.

Use the `service_role` key only in `backend/.env` on trusted machines. Do not put Supabase keys in the
GWToolbox plugin UI, the DLL, or committed files.

## Windows Bridge

Run this on the Gaming PC:

```powershell
python -m backend.windows_bridge.app
```

Then in the Playmate plugin:

- `Local backend URL`: `http://127.0.0.1:8787`
- `Write local JSONL capture`: on
- `Send telemetry to backend`: on, after local logs look sane

The bridge rejects known noisy event types before touching Supabase. For v1 this suppresses
`quest_added` and `quest_details_changed` until quest text decoding and de-duplication are fixed.

Smoke test without GW1:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\smoke-test-bridge.ps1
```

## Hermes Daemon

Run this on the Mac Mini after installing the same requirements:

```bash
python -m backend.hermes_daemon.daemon
```

By default the daemon polls Supabase with stored row-id watermarks instead of opening a Realtime
connection. This keeps the project inside Supabase's free-tier Realtime connection limits while still
letting Supabase act as the audit and memory store. It reads new `game_logs` and `environment_alerts`
rows, keeps recent context in RAM, asks Ollama for a small JSON decision, and inserts approved lines
into `companion_replies`.

`HERMES_ENABLE_REALTIME=true` is supported for low-latency playtests. Hermes uses one planned
Realtime connection for its single Supabase channel; keep `HERMES_REALTIME_CONNECTION_BUDGET=150` to
preserve a 50-connection buffer under Supabase's 200-connection free-tier limit. When Realtime is off,
tune `HERMES_POLL_IDLE_SECONDS`, `HERMES_POLL_ACTIVE_SECONDS`, and `HERMES_POLL_ACTIVE_WINDOW_SECONDS`
instead of adding more Realtime clients.

For the first closed-loop test, leave `HERMES_USE_OLLAMA=false`. In this fallback mode Hermes replies
deterministically to party `player_chat` rows, which proves the Supabase round trip without involving
model setup. Set `HERMES_USE_OLLAMA=true` when the pipe is proven and Ollama is ready on the Mac Mini.

Replies are written to `companion_replies`, not back into `game_logs`, and include `trigger_log_id`
when the source Supabase row is available.

### Optional local TTS

For stable local voice, run a Kokoro-compatible TTS server on the Mac Mini and set:

```bash
HERMES_TTS_PROVIDER=kokoro
KOKORO_TTS_URL=http://127.0.0.1:8880/v1/audio/speech
KOKORO_TTS_VOICE=af_heart
HERMES_TTS_STORAGE_BUCKET=playmate-tts
HERMES_TTS_SIGNED_URL_SECONDS=600
```

For experimental expressive voice, run the project-owned Chatterbox Turbo service and set:

```bash
HERMES_TTS_PROVIDER=chatterbox-turbo
CHATTERBOX_TTS_URL=http://127.0.0.1:4123/v1/audio/speech
CHATTERBOX_TTS_VOICE_SAMPLE=/local/path/azele.wav
CHATTERBOX_TTS_FORMAT=wav
```

Run `backend/supabase/setup.sql` once so the private `playmate-tts` Storage bucket exists. Hermes uses
the service-role key to upload audio and create signed URLs; the plugin only receives the signed URL.
If Chatterbox, Kokoro, or Storage fails, Hermes still writes the text reply and the plugin falls back to local speech.

## Closed-loop smoke test

1. Start the Windows bridge.
2. Start the Mac Mini Hermes daemon with `HERMES_USE_OLLAMA=false`.
3. From the Windows PC, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\smoke-test-bridge.ps1
```

Expected result:

- the bridge returns `{ "accepted": true }` for the synthetic `player_chat`;
- Supabase receives one `game_logs` row;
- Hermes inserts one `companion_replies` row;
- the bridge returns that reply once from `GET /v1/playmate/replies` and marks it consumed.

After that passes, run the same loop from GW1 by enabling `Send telemetry to backend` and `Inject
companion replies into party chat` in the Playmate panel.

## Proactive radar

The plugin can emit `environment_alert` telemetry while in explorable areas. V1 alerts are transition
based rather than continuous spam:

- `enemy_patrol_nearby` when a living enemy enters close range;
- `combat_started` when combat-like state begins;
- `danger_spike` when several enemies are close;
- `combat_over` when combat clears.

These alerts are stored in `environment_alerts`; Hermes consumes them through the free-tier polling
loop by default and writes any companion line to `companion_replies`.

## Memories

Longer-term memory is stored in `memories`, keyed by `character_name` rather than by a hardcoded
character-specific table. Memory rows are intended for compact, useful session summaries rather than
raw chat/log duplication.

Good memory candidates include:

- mission or explorable-session summaries;
- rare or notable item drops;
- quest decisions and progress;
- recurring companion/player preferences;
- map, quest, and source-log ranges that make the memory traceable later.

Embeddings are optional. The first pass can store plain summaries and metadata; vector search can be
enabled once the summarizer is stable.

## Supabase

Run `backend/supabase/setup.sql` in the Supabase SQL editor. It is written to be idempotent and only
adds minimal compatibility columns/publication membership needed by this backend. For free-tier safety,
only `game_logs` and `environment_alerts` are kept in the `supabase_realtime` publication; replies and
memories are read over REST/service-role calls instead.

Keep `service_role` or secret keys out of the plugin and out of git.
