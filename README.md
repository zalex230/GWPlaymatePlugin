# GWPlaymatePlugin

GWPlaymatePlugin is the standalone home for the Playmate GWToolbox++ plugin and its companion backend services.

Playmate is an experimental sensory layer for Guild Wars 1 companions. The plugin runs inside GWToolbox++, captures useful local game context through GWCA, writes JSONL audit logs, and can hand approved telemetry to a local bridge service. The backend bridge and Hermes daemon then provide the closed loop for companion replies.

```text
GW1 + GWToolbox++ Playmate.dll
        -> Windows localhost bridge
        -> Supabase audit/memory tables
        -> Hermes daemon / local model
        -> companion_replies
        -> Playmate local chat rendering
```

This repository intentionally does not vendor the full GWToolbox++ source tree. To build `Playmate.dll`, point the build helper at a separate GWToolbox++ checkout.

## Repository Layout

- `plugins/Playmate/` contains the C++ plugin source and local log review tool.
- `backend/windows_bridge/` exposes the plugin-compatible HTTP API on `127.0.0.1:8787`.
- `backend/hermes/` watches Supabase, keeps bounded world state, exposes Hermes health/event endpoints, writes companion replies, and maintains compact memories.
- `backend/hermes_daemon/` is a compatibility wrapper for older launch commands.
- `backend/shared/` contains shared Pydantic models, constants, throttling, and state helpers.
- `backend/supabase/` contains SQL setup for the expected tables and realtime publication.
- `backend/tests/` covers bridge validation, Hermes behavior, state, and throttling.
- `mac/HermesBridgeControl/` contains a small macOS controller for Hermes and Kokoro LaunchAgents.
- `windows/ClientBridgeControl/` contains a small Windows controller for the local client bridge.
- `tools/build-plugin.ps1` stages the plugin into a GWToolbox++ checkout and builds the DLL.

## Playmate Plugin

Playmate is the in-game sensory layer for the companion system. It is designed to support whichever companion persona a player wants to run, whether that is a roleplay character, a tactical guide, a lore-aware party member, or a test persona used during development. It listens to GW1 through GWCA, turns useful game state into structured telemetry, and provides a safe local path for companion replies to appear in the party chat window.

Playmate currently captures:

- outgoing player party chat;
- selected in-game chat-log events;
- map load/change events;
- quest add/detail-change events;
- periodic map and active-quest snapshots;
- NPC speech bubbles, including allied or quest NPCs traveling with the party;
- notable item drops and rarity signals;
- proactive environment radar alerts in explorable areas.

For early tuning, telemetry is written locally as JSON Lines:

```text
Documents/GWToolboxpp/<computer>/Playmate/telemetry-yyyy-mm-dd.jsonl
```

Local capture is intentionally the default. It lets you play GW1, inspect what the plugin sees, and trim noisy events before sending anything to Supabase.

The emitted `persona` is derived from the active Guild Wars character name at runtime, so the same plugin can support any character/persona without recompiling.

The plugin can also POST events to the local client bridge:

- `POST /v1/playmate/events` receives telemetry JSON.
- `GET /v1/playmate/replies` returns companion replies and optional signed audio URLs.

Replies are injected locally with `GW::Chat::WriteChat`, using the active companion persona as the sender. This writes to the client chat window; it does not send a message to ArenaNet servers.

When `Show companion speech bubbles` is enabled, replies also render as a local speech bubble over the active character's head. This uses the client-side speech bubble UI path and is only visible locally.

NPC speech bubble capture ignores the active player character's own bubble so local companion replies do not loop back into Hermes, but allied NPCs, henchmen, and quest NPCs remain eligible dialogue sources.

The Playmate panel shows the current message lifecycle: whether the last event was accepted by the local bridge, whether the companion is waiting on Hermes/LLM interpretation, and when the last reply arrived.

Environment radar sweeps run only in explorable areas. They emit transition-style `environment_alert` telemetry for nearby enemies, combat start, danger spikes, and combat ending, instead of streaming constant raw agent snapshots.

## Build The Plugin

Prerequisites:

- Windows with Visual Studio Build Tools and CMake configured for the GWToolbox++ project.
- A separate GWToolbox++ checkout that can already configure/build plugins.

Build Playmate against that checkout:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build-plugin.ps1 `
  -GWToolboxRoot C:\Path\To\GWToolboxpp `
  -Configuration RelWithDebInfo
```

To also copy the built DLL into a GWToolbox plugin folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build-plugin.ps1 `
  -GWToolboxRoot C:\Path\To\GWToolboxpp `
  -Configuration RelWithDebInfo `
  -InstallTo C:\Path\To\GWToolboxpp\ComputerName\plugins
```

The script copies `plugins/Playmate` from this repo into `<GWToolboxRoot>\plugins\Playmate`, configures the external GWToolbox++ project if needed, builds the `Playmate` target, and optionally installs `Playmate.dll`.

## Run The Backend

Create a Python environment and install dependencies:

```powershell
python -m venv .venv-playmate
.\.venv-playmate\Scripts\Activate.ps1
pip install -r backend\requirements.txt
```

Create local backend secrets from the template:

```powershell
Copy-Item backend\.env.example backend\.env
```

Fill in your own Supabase URL and server-side key. Do not commit `backend/.env`; it is ignored by Git.

Run the Windows bridge:

```powershell
python -m backend.windows_bridge.app
```

Or use the Windows controller:

```text
windows\ClientBridgeControl\Start Client Bridge Control.cmd
```

Opening the controller starts the client bridge. Closing it stops the bridge.

Run Hermes on the machine that hosts the local model or fallback daemon:

```bash
python -m backend.hermes.daemon
```

For the first plumbing test, leave `HERMES_USE_OLLAMA=false`. Once the closed loop works, set `HERMES_USE_OLLAMA=true` and configure `OLLAMA_HOST` / `OLLAMA_MODEL`.

Optional Kokoro TTS audio needs three pieces:

```bash
bash backend/scripts/install-kokoro-launchagent.sh
python -m backend.scripts.ensure_tts_storage_bucket
python -m backend.hermes.daemon
```

Then set `HERMES_TTS_PROVIDER=kokoro` in `backend/.env`. The Kokoro script installs a local macOS
LaunchAgent for Kokoro-FastAPI on `127.0.0.1:8880`; the storage script creates the private Supabase
bucket configured by `HERMES_TTS_STORAGE_BUCKET`. Keep `backend/.env` local because it contains
service-role credentials.

## Test

```powershell
python -m unittest discover -s backend\tests -p "test_*.py"
```

Bridge smoke test:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\smoke-test-bridge.ps1
```

Local Playmate log review:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\Playmate\tools\review-logs.ps1
```

## Playtest Workflow

1. Launch Guild Wars and GWToolbox++.
2. Open GWToolbox Settings > Plugins and load `Playmate.dll`.
3. Open the Playmate panel and keep `Enable telemetry` and `Write local JSONL capture` enabled.
4. Start the client bridge with `windows\ClientBridgeControl\Start Client Bridge Control.cmd`.
5. Enable `Send telemetry to backend` and `Inject companion replies into party chat` when the bridge is healthy.
6. Change maps, enter an explorable area, send party chat lines, and let several snapshots record.
7. Review local JSONL logs and compare them against Supabase `game_logs`, `environment_alerts`, `companion_replies`, and `memories`.

## Safety Notes

- Keep Supabase service credentials out of the C++ plugin and committed files.
- Keep local JSONL capture enabled during early playtesting so raw behavior can be audited.
- Do expensive work outside the game process.
- Rate-limit proactive alerts so the system stays useful and cheap.
- Treat live plugin use as experimental. GWToolbox plugins are not officially permitted by ArenaNet.
