# Client Bridge Control

Small Windows controller for the GWPlaymate local client bridge.

- Opening the app starts `python -m backend.windows_bridge.app`.
- Closing or quitting the app stops matching bridge processes.
- The **Restart** button stops and starts the bridge if it gets stuck.
- The status panel checks `http://127.0.0.1:8787/health`.

Run it by double-clicking:

```text
windows\ClientBridgeControl\Start Client Bridge Control.cmd
```

The bridge needs `backend/.env` and the Python environment described in the top-level README.
