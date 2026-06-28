# Playmate for GWToolbox++

Playmate is an experimental GWToolbox++ plugin for building in-character AI companions inside Guild Wars 1.

The plugin is the in-game sensory layer for that system. It is designed to support whichever companion persona a player wants to run, whether that is a roleplay character, a tactical guide, a lore-aware party member, or a test persona used during development. It listens to GW1 through GWCA, turns useful game state into structured telemetry, and provides a safe local path for companion replies to appear in the party chat window.

## What It Does Today

Playmate currently captures:

- outgoing player party chat
- selected in-game chat-log events
- map load/change events
- quest add/detail-change events
- periodic map and active-quest snapshots
- NPC speech bubbles, including allied or quest NPCs traveling with the party
- proactive environment radar alerts in explorable areas

For early tuning, telemetry is written locally as JSON Lines:

```text
Documents/GWToolboxpp/<computer>/Playmate/telemetry-yyyy-mm-dd.jsonl
```

This local capture mode is intentionally the default. It lets us play GW1, inspect what the plugin sees, and trim noisy events before sending anything to a cloud backend.

The emitted `persona` is derived from the active Guild Wars character name at runtime, so the same plugin can support any character/persona without recompiling.

The plugin can also POST events to a local companion service:

- `POST /v1/playmate/events` receives telemetry JSON.
- `GET /v1/playmate/replies` returns either `{"replies":["..."]}` or a plain text reply.

Replies are injected locally with `GW::Chat::WriteChat`, using the active companion persona as the sender. This writes to the client chat window; it does not send a message to ArenaNet servers.

When `Show companion speech bubbles` is enabled, replies also render as a local speech bubble over the active character's head. This uses the client-side speech bubble UI path and is only visible locally.

NPC speech bubble capture ignores the active player character's own bubble so local companion replies do not loop back into Hermes, but allied NPCs, henchmen, and quest NPCs remain eligible dialogue sources.

The Playmate panel shows the current message lifecycle: whether the last event was accepted by the local bridge, whether the companion is waiting on Hermes/LLM interpretation, and when the last reply arrived.

Environment radar sweeps run only in explorable areas. They emit transition-style `environment_alert` telemetry for nearby enemies, combat start, danger spikes, and combat ending, instead of streaming constant raw agent snapshots.

## First Playtest Workflow

1. Launch Guild Wars and GWToolbox++.
2. Open GWToolbox Settings > Plugins and load `Playmate.dll`.
3. Open the Playmate panel and keep `Enable telemetry` and `Write local JSONL capture` enabled.
4. Leave `Send telemetry to backend` disabled while reviewing local signal quality, or enable it when the local bridge is running.
5. Change maps, enter an explorable area, send a few party chat lines, and let several snapshots record.
6. Review the newest local log:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\Playmate\tools\review-logs.ps1
```

The review script summarizes event counts by event type, channel, map, and persona; samples representative messages; identifies repeated message patterns; and prints a starter filter matrix.

## Where It Is Going

The intended architecture is:

```text
GW1 + GWToolbox++ Playmate plugin
        -> local JSONL capture for inspection
        -> local or LAN companion service
        -> Supabase game_logs / environment_alerts / companion_replies / companion memory storage
        -> LLM-driven in-character responses
        -> local in-game chat rendering
```

Next milestones:

- Tune the client-side filter matrix so trade spam, combat noise, and ordinary item chatter stay out.
- Decode quest/map text so payloads carry readable names instead of encoded GW strings.
- Add rare loot and quest-state enrichment.
- Promote the local event schema into the Supabase ingestion service once the telemetry shape is stable.

## Design Guardrails

- Keep Supabase service credentials out of the injected DLL.
- Do expensive work outside the game process.
- Prefer local capture and review before cloud ingestion.
- Rate-limit proactive alerts so the system stays useful and cheap.
- Keep the companion grounded in actual GW1 context, not a generic chatbot loop.
