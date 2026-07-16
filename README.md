# Sunmi Print Hub

Turns the built-in 58mm thermal printer on a **Sunmi V2 Pro** into a network print hub,
reachable four ways:

1. **Local manual print**, on-device UI with live preview.
2. **HTTP API (LAN)**, `POST /print`, `GET /status`, `GET /formats`.
3. **MQTT**, with built-in Home Assistant auto-discovery.
4. **Internet listener**, outbound WebSocket to the companion Python server.

Plus a **companion Python (FastAPI) server** that runs on your own infrastructure,
relays jobs to the app over WebSocket, and hosts a web UI with a pixel-accurate
live preview.

## Repository layout

```
android/   Kotlin app for the Sunmi V2 Pro (minSdk 25 / Android 7.1)
server/    FastAPI companion server + web UI (Python 3.10+)
```

See `android/README.md` and `server/README.md` for build/run instructions.

## Platform notes (why this looks "old")

The target device runs **SUNMI OS on Android 7.1 (API 25)**. Consequences that
are intentional, not mistakes:

- **Cleartext HTTP is allowed by default** (pre-Android 9), the LAN HTTP server
  serves plain HTTP with no network-security-config. This is expected on this device.
- **`targetSdk` is kept at 28**, not a modern value, jumping targetSdk forward
  silently opts into background-execution limits (API 26+), scoped storage (API 29+),
  and notification-channel changes that don't reflect how this device actually runs.
- **No Doze / background limits** apply at API 25, a plain foreground `Service`
  hosting the HTTP server + MQTT + WebSocket just runs persistently. We still request
  battery-optimization exemption defensively.
- **No Jetpack Compose**, classic XML layouts + AppCompat only.
- **Internal storage / SQLite only**, no runtime storage permission, no scoped storage.

## Access code

Everything network-facing (HTTP, MQTT, internet listener) is gated by a single
configurable static access code. Local on-device printing is not, it's already
gated by physical access to the device.
