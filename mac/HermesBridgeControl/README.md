# Hermes Bridge Control

Small local macOS controller for the GWPlaymate Hermes bridge.

- Opening the app starts the Hermes daemon and Kokoro LaunchAgents.
- Closing or quitting the app stops them.
- The **Restart Hermes** button runs `launchctl kickstart -k` for `com.gwplaymate.hermes-daemon`.

Build:

```sh
./mac/HermesBridgeControl/build.sh
```

The built app is written to:

```text
mac/HermesBridgeControl/build/Hermes Bridge Control.app
```
