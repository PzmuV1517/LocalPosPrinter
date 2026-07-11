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
export ACCESS_CODE=1234      # MUST match the app's access code
export PRINT_WIDTH=384       # MUST match the app's print width

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then:
- Browser → `http://localhost:8000/` (the web UI)
- POS app → point its "Internet listener" at this host; it connects to
  `wss://<domain>/messages?code=<ACCESS_CODE>`.

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
| POST | `/preview` | Render a payload → PNG (used by the UI, debounced) |
| POST | `/print` | Render → push to device as `image_raw_bitmap` |
| GET | `/status` | Device connected? pending jobs? |
| WS | `/messages` | POS app connects here (`?code=<ACCESS_CODE>`) |

## The 1:1 preview guarantee

`/preview` and `/print` call the **same** `render()` in `app/render.py`. Print ships that
render's exact pixels as `image_raw_bitmap`, so what the preview shows is what prints.
(The app's own on-device Canvas renderer — used for local manual prints and raw
HTTP/MQTT jobs — is kept aligned but isn't part of this pixel-exact guarantee.)
