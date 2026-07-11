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

## The 1:1 preview guarantee

`/preview` and `/print` call the **same** `render()` in `app/render.py`. Print ships that
render's exact pixels as `image_raw_bitmap`, so what the preview shows is what prints.
(The app's own on-device Canvas renderer — used for local manual prints and raw
HTTP/MQTT jobs — is kept aligned but isn't part of this pixel-exact guarantee.)
