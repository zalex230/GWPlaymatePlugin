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
- `backend/hermes_daemon/` watches Supabase, keeps bounded world state, and writes companion replies.
- `backend/shared/` contains shared Pydantic models, constants, throttling, and state helpers.
- `backend/supabase/` contains SQL setup for the expected tables and realtime publication.
- `backend/tests/` covers bridge validation, Hermes behavior, state, and throttling.
- `tools/build-plugin.ps1` stages the plugin into a GWToolbox++ checkout and builds the DLL.

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

Run Hermes on the machine that hosts the local model or fallback daemon:

```bash
python -m backend.hermes_daemon.daemon
```

For the first plumbing test, leave `HERMES_USE_OLLAMA=false`. Once the closed loop works, set `HERMES_USE_OLLAMA=true` and configure `OLLAMA_HOST` / `OLLAMA_MODEL`.

## Test

```powershell
python -m unittest discover -s backend\tests -p "test_*.py"
```

Bridge smoke test:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\smoke-test-bridge.ps1
```

## Safety Notes

- Keep Supabase service credentials out of the C++ plugin and committed files.
- Keep local JSONL capture enabled during early playtesting so raw behavior can be audited.
- Treat live plugin use as experimental. GWToolbox plugins are not officially permitted by ArenaNet.
