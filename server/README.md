# Sunmi Print Hub — companion server

FastAPI server that (a) accepts the POS app's outbound WebSocket connection and pushes
jobs to it, and (b) hosts a web UI for composing prints with a **pixel-accurate live
preview**. Runs on your own infrastructure, behind a TLS-terminating reverse proxy.

## Run

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure (defaults shown):
export ACCESS_PASSWORD=1234  # master password; MUST match the app's access password
export PRINT_WIDTH=384       # MUST match the app's print width
# (ACCESS_CODE is still read as a fallback for the old name.)

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then:
- Browser → `http://localhost:8000/` (the web UI) or `/admin` (admin portal)
- POS app → point its "Internet listener" at this host; it connects to
  `wss://<domain>/messages?password=<ACCESS_PASSWORD>`.

## Set the server password

The **master password** is read from the `ACCESS_PASSWORD` environment variable at startup
(legacy name `ACCESS_CODE` is still accepted). **If unset it defaults to `1234` — change it.**
It must match the access password configured in the app.

```bash
# one-off (this shell only)
export ACCESS_PASSWORD='choose-a-strong-secret'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Make it persist across restarts by baking it into however you run the server:

```ini
# systemd unit — /etc/systemd/system/printhub.service
[Service]
Environment=ACCESS_PASSWORD=choose-a-strong-secret
Environment=PRINT_WIDTH=384
ExecStart=/path/to/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
WorkingDirectory=/path/to/server
```

```yaml
# docker compose
services:
  printhub:
    environment:
      ACCESS_PASSWORD: choose-a-strong-secret
      PRINT_WIDTH: "384"
```

To change it later: update the value and restart the server. There's no password stored in a
file — it lives only in the environment, so nothing to edit besides the variable. (Note: the
temporary limited-use passwords created in `/admin` *are* persisted, under `DATA_DIR`.)

## Passwords & the admin portal

- The **master password** prints without limit and unlocks `/admin`.
- In `/admin` (sign in with the master password) you can:
  - **Create limited-use passwords** — each has a `user` label (who you gave it to) and a
    `max_uses` cap. Passwords may contain letters, numbers, and symbols.
  - See **all active passwords** with their usage (`used / max`, remaining) and **revoke** them.
  - Browse the **print history** (time, format, user, label, status).
- On the compose page the **Check** button next to the password reports
  `No usage limit`, `X usages left`, or `Invalid password` without consuming a use.
- Printing with a temporary password consumes one use and the response reports the
  remaining count; once exhausted the password is rejected.

State (passwords + history) persists to JSON files under `DATA_DIR` (default `server/data/`).

### Fonts

Rendering uses one explicit TTF for host-independent output. Drop a monospace TTF at
`app/fonts/DejaVuSansMono.ttf` (and optionally `-Bold`). Override with `FONT_PATH` /
`FONT_PATH_BOLD`. Without a TTF it falls back to Pillow's tiny bitmap font (fine for a
smoke test, not for real prints).

```bash
# Debian/Ubuntu example:
cp /usr/share/fonts/truetype/dejavu/DejaVuSansMono*.ttf app/fonts/
```

## Reverse proxy (TLS)

Put nginx/Caddy in front so the app hits `wss://<domain>/messages` and browsers hit
`https://<domain>/`. Example Caddy:

```
pos.example.com {
    reverse_proxy localhost:8000
}
```

Caddy proxies WebSockets transparently; the Python process speaks plain HTTP/WS behind it.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web UI |
| GET | `/admin` | Admin portal (master password) |
| POST | `/preview` | Render a payload → PNG (used by the UI, debounced) |
| POST | `/print` | Render → push to device as `image_raw_bitmap` (consumes a use) |
| POST | `/check` | Non-consuming password check |
| POST | `/admin/state` | History + active passwords (master password) |
| POST | `/admin/create` | Create a limited-use password (master password) |
| POST | `/admin/revoke` | Revoke a password (master password) |
| GET | `/status` | Device connected? pending jobs? |
| WS | `/messages` | POS app connects here (`?password=<ACCESS_PASSWORD>`) |

## MUIE — Minimal Unified Incident Envelope (alert system)

A tiny standard your local services use to print a uniform alert. The envelope (fixed
header + footer) sandwiches your message:

```
            ALERT
           <TYPE>
- - - - - - - - - - -      (dash rule)
        <message>
- - - - - - - - - - -
         * * * *           (short star rule)
      <service>
   sent:  <sender time>
   recv:  <app time>
         * * * *
```

`alert_type` is a syslog/journald severity: **emerg, alert, crit, err, warning, notice,
info, debug**. The footer prints two clocks — the time the sender reported (`sent_at`,
epoch seconds) and the time the receiving app stamped it (`recv`).

Two ways to fire one, both take the same fields:

**Via the server (TLS at your reverse proxy), which relays to the device:**

```bash
curl -sX POST https://pos.example.com/alert \
  -H 'Content-Type: application/json' \
  -d '{"password":"'"$ACCESS_PASSWORD"'","alert_type":"crit",
       "service":"backup.service","message":"Snapshot failed: disk 98% full",
       "sent_at":'"$(date +%s)"'}'
```

**Directly to the device on the LAN (plain HTTP — the device is Android 7.1):**

```bash
curl -sX POST http://192.168.1.50:8080/print \
  -H 'Content-Type: application/json' \
  -d '{"password":"'"$ACCESS_PASSWORD"'","format":"alert","alert_type":"warning",
       "service":"diskmon","text":"/var at 90%","sent_at":'"$(date +%s)"'}'
```

Both render the identical envelope. The server path also honours limited-use passwords
(and only consumes a use once the alert actually reaches the device).

## The 1:1 preview guarantee

`/preview` and `/print` call the **same** `render()` in `app/render.py`. Print ships that
render's exact pixels as `image_raw_bitmap`, so what the preview shows is what prints.
(The app's own on-device Canvas renderer — used for local manual prints and raw
HTTP/MQTT jobs — is kept aligned but isn't part of this pixel-exact guarantee.)
