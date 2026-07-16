# Sunmi Print Hub, Android app

Kotlin app for the **Sunmi V2 Pro** (SUNMI OS / Android 7.1, API 25).

## Build

```bash
cd android
# Uses the Gradle wrapper (Gradle 7.5, AGP 7.4.2, Kotlin 1.8.22).
./gradlew assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

> This repo ships the wrapper config but not the `gradlew` binary/jar. Generate it once
> with a local Gradle 7.5 (`gradle wrapper --gradle-version 7.5`) or open the `android/`
> folder in Android Studio, which will create it for you.

Install to a connected device:

```bash
./gradlew installDebug
```

## Architecture

| Piece | File |
|-------|------|
| Application / DI holder | `PrintHubApp.kt`, `core/Hub.kt` |
| Shared job pipeline | `core/PrintDispatcher.kt` |
| Payload schema | `model/PrintPayload.kt` |
| Bitmap renderer | `render/ReceiptRenderer.kt` (+ `ImageUtils`, `CodeRenderer`) |
| Woyou AIDL binding | `aidl/woyou/aidlservice/jiuiv5/*.aidl`, `printer/PrinterManager.kt` |
| Job log (SQLite) | `db/JobLog.kt` |
| Settings (SharedPreferences) | `settings/Settings.kt` |
| HTTP server | `net/HttpServer.kt` |
| MQTT + HA discovery | `net/MqttManager.kt` |
| Internet WebSocket listener | `net/InternetListener.kt` |
| Foreground service | `service/PrintHubService.kt` |
| Boot start | `boot/BootReceiver.kt` |
| UI | `ui/MainActivity`, `ui/SettingsActivity`, `ui/JobLogActivity` |

## Channels

- **HTTP** (default `:8080`): `POST /print`, `GET /status`, `GET /formats`.
  Password via `X-Access-Password` header or `password` field (legacy `X-Access-Code`/`code`
  still accepted). Cleartext HTTP is expected on API 25.
- **MQTT**: subscribes `<prefix>print`, publishes `<prefix>status` (retained, LWT `offline`)
  and `<prefix>lastjob`. Publishes Home Assistant discovery under `homeassistant/…`
  (a `notify` entity with the access code baked into its `command_template`, and a last-job `sensor`).
- **Internet listener**: outbound `wss://<domain>/messages?code=<code>` to the companion server,
  auto-reconnecting with exponential backoff.
- **Local**: on-device manual print screen with live preview (no access code required).

## Boxed border styles

The `boxed` format takes an optional `border_style` (default `line` = a drawn rectangle):
`dashes`, `equals`, `asterisk`, `at`, `hash`, `dot`, `plus`, `wave` (plain-character grids),
and `box`, `double`, `rounded` (Unicode box-drawing). The plain-character styles are the
safe default everywhere; the Unicode ones depend on the device's monospace font actually
having those glyphs (they render fine on the server's DejaVu/Menlo). `GET /formats` lists
the full set.

## Always-on while locked

The foreground service holds a **partial `WakeLock`** and a **high-perf `WifiLock`** for its
whole lifetime (`service/PrintHubService.kt`). This keeps the CPU and Wi-Fi radio awake when
the screen is off / the device is locked, so the internet WebSocket listener and MQTT client
stay connected and pushed jobs still print. Printing itself needs no screen, the Woyou AIDL
service and bitmap rendering run regardless of lock state.

This is deliberate for a mains-powered POS hub. Combine it with the **"Ignore battery
optimizations"** shortcut in Settings and **Start on boot** for a truly unattended listener.

## Notes

- `targetSdk` is intentionally **28**, `minSdk 25`, see the root README for why.
- Recommended image size: keep source images near the **384px** print width so base64
  payloads stay well under typical MQTT broker limits (often ≤1 MB). The HTTP endpoint
  caps bodies at 2 MB.
