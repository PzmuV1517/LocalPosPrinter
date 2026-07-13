# Sunmi Print Hub — companion server + Watchtower

FastAPI server that:

1. **Print relay** — accepts the POS app's outbound WebSocket and pushes jobs to it, and hosts
   a web UI for composing prints with a **pixel-accurate live preview**.
2. **Watchtower** — a fleet **error/log dashboard**. Small **Scout** clients on your other
   devices sign log events to `/ingest`; anything at `err` severity or worse is **auto-printed**,
   and everything is browsable in the login-gated `/watchtower` dashboard.

Runs on your own infrastructure, behind a TLS-terminating reverse proxy (you're exposed at
`watchtower.andreibanu.com`, so TLS is mandatory — see [Security](#security-model)).

## Run

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Config is done in the BROWSER on first run — the env vars below are only bootstrap defaults
# for headless deploys. All of them are editable later in the Settings tab and persist in the
# database, so a `git pull` + restart never re-prompts or resets you.
export SERVER_SECRET_KEY=          # (optional) at-rest encryption key; auto-generated if unset
export ACCESS_PASSWORD=            # (optional) seed the master password headlessly; else the
                                   #   browser setup wizard runs on first visit
export PRINT_WIDTH=384             # bootstrap default; editable in Settings
export AUTO_PRINT_MIN_SEV=err      # logs at this severity or worse auto-print
export AUTO_PRINT_MAX_PER_MIN=30   # runaway fuse (0 = unlimited); not per-message dedup
export LOG_RETENTION_DAYS=30       # Watchtower log retention
export HMAC_SKEW_SECS=300          # allowed client/server clock skew

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `https://<your-domain>/` — on first run you'll get a **setup wizard** (master password
+ defaults); after that it's the login-gated **Watchtower** dashboard.

## Security model

- **Machine clients** (Scouts, print services, the printer app) authenticate with **HMAC-signed
  requests** — never a shared password in a URL. Each carries `X-Device-Id`, `X-Timestamp`,
  `X-Nonce` and `X-Signature`, where the signature is
  `HMAC-SHA256(device_secret, "device_id\ntimestamp\nnonce\nMETHOD\npath\nsha256_hex(body)")`.
  The server rejects requests with a stale timestamp (±`HMAC_SKEW_SECS`) or a replayed nonce.
- **Device secrets** are stored **encrypted** at rest (Fernet, key from `SERVER_SECRET_KEY` or an
  auto-generated `data/server.key`, mode 0600). They're recoverable only by the server because
  HMAC verification needs them; they are shown to you **once** at issue time and never again.
- **Temp print passwords** are one-way **scrypt-hashed** (found via a keyed lookup hash).
- **The operator** logs into `/watchtower` with the master password and receives a signed
  **session token** (kept in the browser's localStorage). The raw password never persists there,
  and the dashboard renders **nothing** until the token verifies.
- **TLS is required in production.** Terminate it at your reverse proxy. The Android app refuses
  plaintext `ws://` to a public host once a device secret is configured.
- Everything security-relevant is written to the server's audit log (`data/server.log` + stdout):
  logins, auth failures, ingests, prints, device issuance/revocation.

## Watchtower — the dashboard (served at `/`)

A single dark-themed page. On first run it shows the **setup wizard**; once configured it shows a
**login gate**, then the dashboard renders nothing until your session token verifies (the token is
kept in `localStorage`; the master password is never stored there). Tabs:

- **Logs** — device cards (online/offline, last-seen, 24h severity breakdown) + a filterable,
  auto-refreshing log stream. Each row has a **Print** button for manual, on-demand printing.
- **Print** — the compose UI with the pixel-accurate live preview (formats, fonts, MUIE alerts,
  barcodes/QR, images). You're already authenticated, so it prints straight from your session.
- **Devices** — *Issue device secret* (shown once), **Rotate**, **Revoke**.
- **Passwords** — create/revoke limited-use print passwords.
- **History** — the print history.
- **Settings** — edit all config (print width, auto-print severity + fuse, retention) and change
  the master password.

### Scout — the log-shipping client

**Easiest install (no git clone)** — the server hosts the client and a one-line installer. In the
**Devices** tab, *Issue device secret*, then on the device run the command shown there:

```bash
curl -fsSL "https://watchtower.andreibanu.com/install-scout?device_id=kitchen-pi" | bash
scout set-secret sph_xxxxxxxx        # the secret shown once in the dashboard
scout -s err --service test "hello watchtower"
```

The installer downloads `scout.py` to `~/.local/share/scout`, writes `~/.config/scout/scout.env`
(with the server URL + device id pre-filled), and drops a `scout` launcher in `~/.local/bin`. It's
re-runnable and never overwrites a secret you've already set.

**Or use `scout.py` directly** (stdlib-only) — issue a secret in the dashboard, then:

```bash
export WATCHTOWER_URL=https://watchtower.andreibanu.com
export SCOUT_DEVICE_ID=kitchen-pi
export SCOUT_SECRET=sph_xxxxxxxx          # shown once when you issued it

python scout.py --severity err --service backup.service "snapshot failed: disk 98% full"
```

…or from your own Python:

```python
from scout import Scout
scout = Scout()                            # reads the env vars above
scout.err("uncaught exception in worker", service="worker", meta={"pid": 4123})
scout.info("nightly job finished", service="cron")
```

`err` and worse auto-print as an MUIE alert; everything is visible in the dashboard. The **printer
app itself** self-reports its own print failures/rejects automatically once it's paired with a
device secret (Settings → *Watchtower pairing*).

### Live presence & remote updates (agent mode)

A one-shot Scout only appears "online" right after it sends a log. Run it as an **agent** for
continuous presence and remote control:

```bash
scout install-service     # writes + enables a systemd --user unit running `scout agent`
loginctl enable-linger "$USER"   # (optional) keep it running after you log out
```

The agent **long-polls** `/agent/poll` (HMAC-signed). That poll is its heartbeat, so the device
shows **online** without sending a log and — because the poll drops and immediately re-polls on a
server restart — it's marked alive again within seconds. The dashboard's device card shows *agent
online* and the running **scout version**.

From the **Devices** tab, **Update** (per device) or **Update all scouts** queues an update; each
agent picks it up on its next poll (near-instant while connected), pulls the latest `scout.py` from
this server, and restarts itself. Since the server serves `scout.py` from its own checkout, run the
server self-update first so scouts pull the newest `main`.

## Pairing the printer app

1. In **Devices**, *Issue device secret* for the printer (e.g. id `pos-front`). Copy the secret.
2. In the app: **Settings → Watchtower pairing**, paste the device id + secret, enable the
   internet listener, set the server domain, Save.

The app now connects to `wss://<domain>/messages` **HMAC-signed** — no password in the URL, and no
password-fallback path. (Any device on the old v1.0.x password link must be re-paired this way.)

## First-run setup & changing config

There's no config file to edit. On first visit the **setup wizard** collects the master password
and the defaults; everything is stored in the DB under `DATA_DIR` and is editable later in the
**Settings** tab. `git pull` + restart keeps all of it. For a headless deploy, set
`ACCESS_PASSWORD` in the environment to seed the master password and skip the wizard:

```ini
# systemd — /etc/systemd/system/watchtower.service
[Service]
Environment=ACCESS_PASSWORD=choose-a-strong-secret
ExecStart=/path/to/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
WorkingDirectory=/path/to/server
Restart=always
```

## Frontend (dashboard)

The dashboard is a **React + TypeScript (Vite)** app in `server/web/`. The built bundle is
committed to `server/web/dist/` and served by FastAPI, so deploys and the git-pull self-update
need **no Node on the server**. To change the UI:

```bash
cd server/web
npm install
npm run dev      # local dev against a running uvicorn (proxies the API)
npm run build    # rebuild dist/ — commit the result so the server picks it up
```

`node_modules/` is gitignored; `dist/` is committed on purpose.

## Updating (self-update button)

**Settings → Server updates → Pull latest & restart** runs `git pull --ff-only origin main`,
`pip install -r requirements.txt` if anything changed, and then restarts the service — no manual
`git pull`. The dashboard shows the git output and reloads once the server is back (your session
survives the restart).

For the restart to bring up the new code, the service must run under a supervisor that restarts
it — systemd with `Restart=always` (above) or Docker `restart: unless-stopped`. By default the
process re-execs itself in place (works under any supervisor, and standalone in most setups); set
`UPDATE_RESTART_CMD` to override, e.g. `Environment=UPDATE_RESTART_CMD=sudo systemctl restart watchtower`.

## Passwords & tabs

- The **master password** logs you into the dashboard and prints without limit.
- **Passwords** tab — create limited-use print passwords (a `user` label + a `max_uses` cap),
  see usage, and revoke. These are for handing to LAN services; a use is consumed only when the
  print actually reaches the device. The **Print** tab's `/check` reports remaining uses without
  consuming one.
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

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/`, `/watchtower` | — | Watchtower SPA (setup wizard → login → tabs) |
| GET | `/setup/status` | — | Is the server configured yet? |
| POST | `/setup` | — (first run only) | Complete first-run setup; returns a session token |
| POST | `/session/login` | master pw | Exchange the master password for a session token |
| POST | `/session/verify` | session | Is this session token still valid? |
| POST | `/config/get`, `/config/set` | session/master | Read / update config (Settings tab) |
| POST | `/preview` | — | Render a payload → PNG |
| POST | `/print` | session **or** master/temp pw **or** HMAC | Render → push to device |
| POST | `/alert` | session **or** master/temp pw **or** HMAC | MUIE alert intake |
| POST | `/check` | — | Non-consuming password check |
| POST | `/ingest` | **HMAC** | Scout log intake; auto-prints `err`+ |
| POST | `/watchtower/logs` | session/master | Filtered logs + device cards + counts |
| POST | `/watchtower/print` | session/master | Manually print a stored log by id |
| POST | `/watchtower/devices/create` \| `/rotate` \| `/revoke` | session/master | Manage device secrets |
| POST | `/admin/state` \| `/admin/create` \| `/admin/revoke` | session/master | Temp passwords + history |
| GET | `/status` | — | Device connected? pending jobs? |
| WS | `/messages` | **HMAC only** | POS app connects here for jobs |

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
