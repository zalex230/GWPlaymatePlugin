# Hermes Bridge Control

Small local macOS controller for the GWPlaymate Hermes bridge.

- Opening the app starts the Hermes daemon and the active TTS LaunchAgent from `backend/.env`.
- Closing or quitting the app stops Hermes and the active TTS LaunchAgent.
- The status view marks the active TTS provider and any installed fallback provider.
- The **Restart Hermes** button runs `launchctl kickstart -k` for `com.gwplaymate.hermes-daemon`.

Build:

```sh
./mac/HermesBridgeControl/build.sh
```

The built app is written to:

```text
mac/HermesBridgeControl/build/Hermes Bridge Control.app
```
