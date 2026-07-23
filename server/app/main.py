"""
Watchtower, FastAPI companion server + fleet error/log dashboard for Sunmi Print Hub.

Two roles in one process:

  1. **Print relay**, the POS app keeps an outbound WebSocket here (``/messages``, HMAC-only);
     the Print tab and LAN services render jobs and we push them to the device.
  2. **Watchtower**, an observability platform. Small **Scout** clients sign log events to
     ``/ingest``; anything at ``err`` severity or worse is auto-printed, and everything is
     browsable in the single-page dashboard served at ``/``.

Setup & config are done **in the browser** on first run (a setup wizard), persisted in SQLite so
pulls/updates never re-prompt. Machine clients authenticate with **HMAC-signed requests**; the
operator logs in with the master password and gets a signed **session token** (localStorage).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import crypto
from . import render as rendermod
from .agents import agents
from .auth import Auth
from .db import Database, sev_num
from .logging_setup import setup as setup_logging
from .relay import relay

# ---------------------------------------------------------------------------
# Config, env values are only *bootstrap defaults*; the source of truth is the DB config table,
# edited via the web Settings/Setup. This is what lets `git pull` + restart keep your settings.
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
HMAC_SKEW_SECS = int(os.environ.get("HMAC_SKEW_SECS", "300"))

_DEF_WIDTH = int(os.environ.get("PRINT_WIDTH", "384"))
_DEF_MIN_SEV = os.environ.get("AUTO_PRINT_MIN_SEV", "err")
_DEF_FUSE = int(os.environ.get("AUTO_PRINT_MAX_PER_MIN", "30"))
_DEF_RETENTION = int(os.environ.get("LOG_RETENTION_DAYS", "30"))

_HERE = os.path.dirname(__file__)
_SERVER_DIR = os.path.dirname(_HERE)
# Built React/Vite bundle (committed to the repo so git-pull self-update needs no Node).
_WEB_DIST = os.path.join(_SERVER_DIR, "web", "dist")

# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
log = setup_logging(DATA_DIR, os.environ.get("LOG_LEVEL", "INFO"))
box = crypto.SecretBox(DATA_DIR)
db = Database(DATA_DIR, box, lookup_key=box.derive("temp-password-lookup"))
auth = Auth(db, skew_secs=HMAC_SKEW_SECS)
from .notify import Notifier  # noqa: E402  (after db/box exist)
from .passkeys import Passkeys  # noqa: E402
from .mqtt_bridge import MqttBridge  # noqa: E402
from .mqtt_client import MqttClientBridge  # noqa: E402
notifier = Notifier(db, box)
passkeys = Passkeys(db)
mqtt_bridge = MqttBridge(db, DATA_DIR, log)
mqtt_client = MqttClientBridge(db, box, log)
from .confer import ConferHub, ConferSessions, ConferConn  # noqa: E402
confer_hub = ConferHub(db)
confer_sessions = ConferSessions(db, auth)

# Longest message Confer will accept (chars). Images are exempt (sent as base64).
CONFER_MAX_CHARS = int(os.environ.get("CONFER_MAX_CHARS", "888"))

# Bootstrap: a headless deploy can skip the wizard by providing BOTH a username and password in
# the env. With only a password (or neither), the browser setup wizard runs on first visit.
if not db.is_configured():
    env_user = os.environ.get("ADMIN_USERNAME")
    env_pw = os.environ.get("ACCESS_PASSWORD") or os.environ.get("ACCESS_CODE")
    if env_user and env_pw:
        auth.set_credentials(env_user, env_pw)
        db.set_config("print_width", _DEF_WIDTH)
        db.set_config("auto_print_min_sev", _DEF_MIN_SEV)
        db.set_config("auto_print_max_per_min", _DEF_FUSE)
        db.set_config("log_retention_days", _DEF_RETENTION)
        log.info("Bootstrapped master credentials from environment; setup wizard skipped.")

_auto_print_times: deque[float] = deque()

app = FastAPI(title="Watchtower, Sunmi Print Hub")
app.mount("/fonts", StaticFiles(directory=os.path.join(_HERE, "fonts")), name="fonts")
_ASSETS_DIR = os.path.join(_WEB_DIST, "assets")
if os.path.isdir(_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")
else:  # dist not built/committed, the SPA won't load, but the API still runs
    log.warning("Web bundle missing at %s, run `npm --prefix web run build`.", _WEB_DIST)

_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}

# Content-Security-Policy: everything self-hosted. 'unsafe-inline' for style only (React inline
# styles); scripts are the bundled /assets file. Blocks framing, external connects, plugins.
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    # Force HTTPS when a proxy tells us the client came in over plain HTTP, so credentials never
    # travel unencrypted even on a first visit before HSTS is remembered. 308 keeps the method/body.
    if request.headers.get("x-forwarded-proto") == "http":
        return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = (
        "publickey-credentials-get=(self), publickey-credentials-create=(self), "
        "geolocation=(), microphone=(), camera=()")
    resp.headers["Content-Security-Policy"] = _CSP
    if request.headers.get("x-forwarded-proto") == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    return resp


# ---- simple in-memory per-IP rate limiter (brute-force protection on auth) ----
_rl_hits: dict[str, list[float]] = {}


def _rate_ok(key: str, ip: str, max_n: int, window: float) -> bool:
    now = time.time()
    k = f"{key}:{ip}"
    hits = [t for t in _rl_hits.get(k, []) if now - t < window]
    _rl_hits[k] = hits
    if len(hits) >= max_n:
        return False
    hits.append(now)
    return True


def _rp_origin(request: Request) -> tuple[str, str]:
    """(rp_id, origin) for WebAuthn, from env override or the (proxy-aware) request host."""
    origin = os.environ.get("WEBAUTHN_ORIGIN") or _public_base_url(request)
    rp_id = os.environ.get("WEBAUTHN_RP_ID") or origin.split("://", 1)[-1].split("/")[0].split(":")[0]
    return rp_id, origin


# ---------------------------------------------------------------------------
# Runtime config accessors (read the DB each time; cheap and always current)
# ---------------------------------------------------------------------------
def print_width() -> int:
    return db.get_int("print_width", _DEF_WIDTH)


def auto_print_max_num() -> int:
    return sev_num(db.get_config("auto_print_min_sev", _DEF_MIN_SEV))


def auto_print_fuse() -> int:
    return db.get_int("auto_print_max_per_min", _DEF_FUSE)


def retention_days() -> int:
    return db.get_int("log_retention_days", _DEF_RETENTION)


def err_retention_days() -> int:
    return db.get_int("err_retention_days", 0)  # 0 = same as retention_days


def disk_alert_pct() -> int:
    return db.get_int("disk_alert_pct", 90)  # 0 disables


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _label_for(payload: dict) -> str:
    return (payload.get("title") or payload.get("text") or payload.get("format") or "print")[:60]


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


async def _read(request: Request):
    raw = await request.body()
    try:
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            data = {}
    except (ValueError, TypeError):
        data = {}
    return raw, data


def _hmac_device(request: Request, raw: bytes):
    res = auth.verify_request(request.method, request.url.path, request.headers, raw)
    if res.ok:
        return res.device_id
    if request.headers.get("x-signature"):
        log.warning("HMAC rejected on %s from %s (%s): %s",
                    request.url.path, _client_ip(request), res.device_id, res.reason)
    return None


def _session_ok(request: Request) -> bool:
    return auth.verify_session(auth.bearer(request.headers.get("authorization")))


def _confer_user(request: Request):
    """Resolve a Confer bearer token to a user row, or None. Confer accounts are separate from
    the master/admin session, a Confer token can't touch the dashboard, and vice versa."""
    uid = confer_sessions.resolve(auth.bearer(request.headers.get("authorization")))
    return db.confer_get_user(uid) if uid else None


def _authed_admin(request: Request, body: dict) -> bool:
    """Operator auth for dashboard endpoints: valid session token OR the master password."""
    return _session_ok(request) or auth.is_master((body.get("password") or "").strip())


def _valid_temp_password(pw) -> bool:
    """A non-revoked temp password with uses left (non-consuming), for the public print page."""
    pw = (pw or "").strip()
    if not pw:
        return False
    row = db.find_temp_password(pw)
    return bool(row and not row["revoked"] and (row["max_uses"] - row["used"]) > 0)


# Public (temp-password) prints are capped so nobody burns the paper roll.
PUBLIC_MAX_CHARS = int(os.environ.get("PUBLIC_MAX_CHARS", "600"))


def _content_len(payload: dict) -> int:
    n = len(str(payload.get("title") or "")) + len(str(payload.get("text") or ""))
    for it in payload.get("items") or []:
        if isinstance(it, dict):
            n += len(str(it.get("label") or "")) + len(str(it.get("value") or ""))
    return n


def _public_limit_error(payload: dict) -> "str | None":
    """Reject oversized / image public prints (temp-password callers) to protect the paper roll."""
    if payload.get("image") or payload.get("image_raw_bitmap"):
        return "Images aren't allowed on public prints"
    if _content_len(payload) > PUBLIC_MAX_CHARS:
        return f"Too long for a public print (max {PUBLIC_MAX_CHARS} characters)"
    return None


def _usage_message(unlimited: bool, remaining) -> str:
    return "no usage limit" if unlimited else f"{remaining} usage{'s' if remaining != 1 else ''} left"


# ---------------------------------------------------------------------------
# Static page (single-page app served at / and /watchtower)
# ---------------------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_WEB_DIST, "index.html"), headers=_NO_CACHE)


@app.get("/watchtower")
async def watchtower_alias() -> FileResponse:
    return FileResponse(os.path.join(_WEB_DIST, "index.html"), headers=_NO_CACHE)


@app.get("/public-print")
async def public_print_page() -> FileResponse:
    # Public page, anyone can open it, but printing only works with a valid temp password.
    return FileResponse(os.path.join(_WEB_DIST, "public-print.html"), headers=_NO_CACHE)


@app.get("/public-print.js")
async def public_print_js() -> FileResponse:
    return FileResponse(os.path.join(_WEB_DIST, "public-print.js"), media_type="text/javascript",
                        headers=_NO_CACHE)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    # Unauthenticated liveness only, no data. Used to detect the server after a restart.
    return JSONResponse({"ok": True})


@app.post("/status")
async def status(request: Request) -> JSONResponse:
    # Authenticated: reveals device/queue state, so it requires a session/master password.
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    connected = relay.is_connected()
    pmeta = _printer_meta()
    return JSONResponse(
        {
            "device_connected": connected,
            # Readiness from the printer's last status frame (paper, cover, faults). Only meaningful
            # while connected; unknown (older app not reporting) counts as ready, no false alarm.
            "printer_ready": bool(pmeta.get("ready", True)) if connected else False,
            "printer_state": (str(pmeta.get("printer_state") or "ready") if connected else "offline"),
            "pending_jobs": sum(len(q) for q in relay.pending.values()),
            "print_width": print_width(),
            # A printer that switched to chat isn't offline, surface it as "in Confer mode".
            # Driven by the mode announcement on the print socket (works even when the Confer
            # server is a different machine). Presence lists who's on THIS server's Confer channel.
            "confer_mode": relay.any_confer(),
            "confer_presence": confer_hub.presence(),
        }
    )


# ---------------------------------------------------------------------------
# Scout self-hosting, install the log-shipping client straight from this server, no git clone.
#   curl -fsSL https://<domain>/install-scout | bash
# ---------------------------------------------------------------------------
def _public_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@app.get("/scout.py")
async def scout_source() -> FileResponse:
    return FileResponse(os.path.join(_SERVER_DIR, "scout.py"), media_type="text/x-python", headers=_NO_CACHE)


_INSTALL_SCRIPT = r"""#!/usr/bin/env bash
# Scout installer for Watchtower, downloads the client from your server and gets it ready
# for a device secret. No git clone, stdlib-only, re-runnable (never clobbers your secret).
set -e
BASE="__BASE__"
DEVICE_ID="__DEVICE__"
BIN="$HOME/.local/bin"
LIB="$HOME/.local/share/scout"
CONF_DIR="$HOME/.config/scout"
CONF="$CONF_DIR/scout.env"

mkdir -p "$BIN" "$LIB" "$CONF_DIR"
echo "Downloading scout.py from $BASE ..."
curl -fsSL "$BASE/scout.py" -o "$LIB/scout.py"

# Config is written once so re-running the installer keeps the existing secret.
if [ ! -f "$CONF" ]; then
  printf 'WATCHTOWER_URL=%s\nSCOUT_DEVICE_ID=%s\nSCOUT_SECRET=\n' "$BASE" "$DEVICE_ID" > "$CONF"
  chmod 600 "$CONF"
else
  echo "Keeping existing config at $CONF"
fi

cat > "$BIN/scout" <<'LAUNCH'
#!/usr/bin/env bash
CONF="$HOME/.config/scout/scout.env"
[ -f "$CONF" ] && set -a && . "$CONF" && set +a
_set() { tmp=$(mktemp); grep -v "^$1=" "$CONF" 2>/dev/null > "$tmp" || true; echo "$1=$2" >> "$tmp"; mv "$tmp" "$CONF"; chmod 600 "$CONF"; }
case "$1" in
  set-secret) _set SCOUT_SECRET "$2"; echo "Secret saved."; exit 0 ;;
  set-device) _set SCOUT_DEVICE_ID "$2"; echo "Device id saved."; exit 0 ;;
esac
exec python3 "$HOME/.local/share/scout/scout.py" "$@"
LAUNCH
chmod +x "$BIN/scout"

# Put ~/.local/bin on PATH for future shells (idempotent across common rc files).
ONPATH=0; case ":$PATH:" in *":$BIN:"*) ONPATH=1 ;; esac
if [ "$ONPATH" = "0" ]; then
  for rc in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.zshrc"; do
    [ -f "$rc" ] || continue
    grep -q 'Watchtower Scout installer' "$rc" 2>/dev/null && continue
    printf '\n# Watchtower Scout installer\nexport PATH="%s:$PATH"\n' "$BIN" >> "$rc"
  done
fi

echo ""
echo "Scout installed: $BIN/scout   (config: $CONF)"

# --- guided setup: prompt on the real terminal even when run via `curl | bash` ---
set +e
TTY=/dev/tty
have_tty=0; [ -r "$TTY" ] && [ -w "$TTY" ] && have_tty=1
conf_set() { tmp=$(mktemp); grep -v "^$1=" "$CONF" 2>/dev/null > "$tmp"; echo "$1=$2" >> "$tmp"; mv "$tmp" "$CONF"; chmod 600 "$CONF"; }
conf_get() { grep "^$1=" "$CONF" 2>/dev/null | head -1 | cut -d= -f2-; }
ask_yn() { p="$1"; d="$2"; a=""; printf "%s " "$p" > "$TTY"; read a < "$TTY" 2>/dev/null; a="${a:-$d}"; case "$a" in [Yy]*) return 0;; *) return 1;; esac; }

# Detect a Proxmox node even as a non-root user (its PATH may lack /usr/sbin, and /etc/pve is a
# fuse mount a plain user can't stat), by also probing absolute paths that are world-readable.
is_proxmox() {
  command -v pveversion >/dev/null 2>&1 || [ -d /etc/pve ] \
    || [ -e /usr/bin/pveversion ] || [ -e /usr/sbin/pveversion ] || [ -d /usr/share/pve-manager ]
}

# On a Proxmox node: forward the host journal AND stand up a syslog receiver, run as a root system
# service (full journal + privileged port 514), and point every running LXC at it. One scout, whole
# node (host + all guests) into Watchtower's error log.
setup_proxmox() {
  echo ""
  echo "Proxmox detected. Configuring node-wide log collection (host + all VMs/LXCs)..."
  conf_set SCOUT_FORWARD_JOURNALD 1
  conf_set SCOUT_FORWARD_SYSLOG 1
  [ -z "$(conf_get SCOUT_FORWARD_MIN_SEV)" ] && conf_set SCOUT_FORWARD_MIN_SEV err
  [ -z "$(conf_get SCOUT_FORWARD_NO_PRINT)" ] && conf_set SCOUT_FORWARD_NO_PRINT 1

  systemctl --user disable --now scout-agent >/dev/null 2>&1 || true
  cat > /etc/systemd/system/scout-agent.service <<UNIT
[Unit]
Description=Watchtower Scout (Proxmox)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$CONF
ExecStart=/usr/bin/python3 $LIB/scout.py agent
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now scout-agent && echo "  scout-agent system service running."

  HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  LINE="*.err  @$HOST_IP:514"
  n=0
  for id in $(pct list 2>/dev/null | awk 'NR>1 && $2=="running"{print $1}'); do
    if pct exec "$id" -- sh -c "command -v rsyslogd >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y rsyslog >/dev/null 2>&1; }; printf '%s\n' '$LINE' > /etc/rsyslog.d/90-watchtower.conf; systemctl restart rsyslog >/dev/null 2>&1 || service rsyslog restart >/dev/null 2>&1" 2>/dev/null; then
      echo "  CT $id -> forwarding errors"; n=$((n+1))
    else
      echo "  CT $id -> skipped (no network for apt, or no systemd/rsyslog)"
    fi
  done
  echo "  configured $n running container(s)."
  echo ""
  echo "VMs are separate OSes; inside each VM run:"
  echo "  echo '$LINE' | sudo tee /etc/rsyslog.d/90-watchtower.conf && sudo systemctl restart rsyslog"
  if pve-firewall status 2>/dev/null | grep -qi enabled; then
    echo "NOTE: Proxmox firewall is on; allow inbound UDP 514 from the guest subnet or guest logs won't arrive."
  fi
}

if [ "$have_tty" = "1" ]; then
  echo ""
  if [ -z "$(conf_get SCOUT_DEVICE_ID)" ]; then
    printf "Device id (from the dashboard): " > "$TTY"; read did < "$TTY" 2>/dev/null
    [ -n "$did" ] && conf_set SCOUT_DEVICE_ID "$did"
  fi
  printf "Paste the device secret from the dashboard (blank = keep current): " > "$TTY"
  stty -echo < "$TTY" 2>/dev/null; read secret < "$TTY" 2>/dev/null; stty echo < "$TTY" 2>/dev/null; printf "\n" > "$TTY"
  [ -n "$secret" ] && { conf_set SCOUT_SECRET "$secret"; echo "Secret saved."; }

  if [ -n "$(conf_get SCOUT_SECRET)" ]; then
    if ask_yn "Send a test log now? [Y/n]" Y; then
      "$BIN/scout" -s info --service setup "scout installed on $(hostname)" >/dev/null 2>&1 \
        && echo "Test log sent, check the dashboard's Logs tab." || echo "Could not reach the server for the test."
    fi
    if is_proxmox; then
      if [ "$(id -u)" = "0" ]; then
        setup_proxmox
      else
        echo ""
        echo "Proxmox detected but not running as root. Re-run this installer as root to auto-configure"
        echo "node-wide logging (host + all VMs/LXCs)."
      fi
    elif ask_yn "Run scout as an always-on background service so it stays online? [Y/n]" Y; then
      "$BIN/scout" install-service
      if ask_yn "Keep it running after you log out (enable linger)? [Y/n]" Y; then
        loginctl enable-linger "$USER" 2>/dev/null && echo "Linger enabled." \
          || echo "Couldn't enable linger automatically, run: sudo loginctl enable-linger \"$USER\""
      fi
    fi
    echo ""
    echo "Done, your device should show 'agent online' in the dashboard shortly."
  else
    echo ""
    echo "No secret set. When you have it:  $BIN/scout set-secret <SECRET>  then  $BIN/scout install-service"
  fi
else
  # No terminal (piped non-interactively). Auto-finish a Proxmox node if the secret is already set.
  if is_proxmox && [ "$(id -u)" = "0" ] && [ -n "$(conf_get SCOUT_SECRET)" ]; then
    setup_proxmox
  else
    echo ""
    echo "Finish setup:"
    [ -z "$DEVICE_ID" ] && echo "  $BIN/scout set-device <DEVICE_ID>"
    echo "  $BIN/scout set-secret <SECRET>"
    if is_proxmox; then
      echo "  # then re-run this installer as root to auto-configure node-wide logging"
    else
      echo "  $BIN/scout install-service        # run as a background service"
      echo "  loginctl enable-linger \"$USER\"   # keep it running after logout"
    fi
  fi
fi

if [ "$ONPATH" = "0" ]; then
  echo ""
  echo "('scout' is on PATH in new shells. For THIS shell: export PATH=\"$BIN:\$PATH\")"
fi
"""


@app.get("/install-scout")
async def install_scout(request: Request) -> PlainTextResponse:
    device_id = (request.query_params.get("device_id") or "").strip()
    script = _INSTALL_SCRIPT.replace("__BASE__", _public_base_url(request)).replace("__DEVICE__", device_id)
    return PlainTextResponse(script, media_type="text/x-shellscript", headers=_NO_CACHE)


# ---------------------------------------------------------------------------
# First-run setup (browser wizard). Refuses once configured.
# ---------------------------------------------------------------------------
@app.get("/setup/status")
async def setup_status() -> JSONResponse:
    return JSONResponse({"configured": db.is_configured()})


@app.post("/setup")
async def setup(request: Request) -> JSONResponse:
    if db.is_configured():
        return JSONResponse({"error": "Already configured"}, status_code=409)
    _, body = await _read(request)
    username = (body.get("username") or "").strip()
    pw = (body.get("master_password") or "").strip()
    if not username:
        return JSONResponse({"error": "Username is required"}, status_code=400)
    if len(pw) < 4:
        return JSONResponse({"error": "Password must be at least 4 characters"}, status_code=400)
    auth.set_credentials(username, pw)
    db.set_config("print_width", int(body.get("print_width") or _DEF_WIDTH))
    db.set_config("auto_print_min_sev", (body.get("auto_print_min_sev") or _DEF_MIN_SEV))
    db.set_config("auto_print_max_per_min", int(body.get("auto_print_max_per_min") or _DEF_FUSE))
    db.set_config("log_retention_days", int(body.get("log_retention_days") or _DEF_RETENTION))
    log.info("Initial setup completed via web wizard from %s (user=%s)", _client_ip(request), username)
    return JSONResponse({"ok": True, "token": auth.login(username, pw)})


# ---------------------------------------------------------------------------
# Session login (browser)
# ---------------------------------------------------------------------------
@app.post("/session/login")
async def session_login(request: Request) -> JSONResponse:
    ip = _client_ip(request)
    if not _rate_ok("login", ip, max_n=10, window=300):
        log.warning("Login rate-limited from %s", ip)
        return JSONResponse({"ok": False, "error": "Too many attempts, try again later"}, status_code=429)
    _, body = await _read(request)
    token = auth.login((body.get("username") or "").strip(), (body.get("password") or "").strip())
    if not token:
        log.warning("Failed dashboard login from %s", ip)
        return JSONResponse({"ok": False, "error": "Invalid username or password"}, status_code=401)
    log.info("Dashboard login from %s", ip)
    return JSONResponse({"ok": True, "token": token})


@app.post("/session/verify")
async def session_verify(request: Request) -> JSONResponse:
    return JSONResponse({"ok": _session_ok(request)})


@app.post("/session/logout")
async def session_logout(request: Request) -> JSONResponse:
    auth.logout(auth.bearer(request.headers.get("authorization")))
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Confer, private printer chat (see confer.py)
# ---------------------------------------------------------------------------
async def _confer_store_and_fanout(chat_id: int, sender: str, display: str,
                                   kind: str, text, image) -> "JSONResponse":
    """Validate + persist + broadcast one message. Shared by participant and admin senders."""
    if not db.confer_chat_exists(int(chat_id) if str(chat_id).isdigit() else -1):
        return JSONResponse({"error": "No such chat"}, status_code=404)
    kind = "image" if kind == "image" else "text"
    if kind == "image":
        body = (image or "").strip()
        if not body:
            return JSONResponse({"error": "Missing image"}, status_code=400)
    else:
        body = (text or "")
        if not body.strip():
            return JSONResponse({"error": "Empty message"}, status_code=400)
        if len(body) > CONFER_MAX_CHARS:
            return JSONResponse({"error": f"Message too long (max {CONFER_MAX_CHARS} characters)"},
                                status_code=400)
    stored = await confer_hub.post_message(int(chat_id), sender, display, kind, body)
    if not stored:
        return JSONResponse({"error": "No such chat"}, status_code=404)
    # Don't echo the (possibly large) image body back in the ack.
    ack = {k: v for k, v in stored.items() if k != "body"}
    return JSONResponse({"ok": True, "message": ack})


# ---- participant endpoints (Confer account token) ----
@app.post("/confer/login")
async def confer_login(request: Request) -> JSONResponse:
    ip = _client_ip(request)
    if not _rate_ok("confer_login", ip, max_n=10, window=300):
        return JSONResponse({"error": "Too many attempts, try again later"}, status_code=429)
    _, body = await _read(request)
    res = confer_sessions.login((body.get("username") or "").strip(), (body.get("password") or "").strip())
    if not res:
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)
    return JSONResponse({"ok": True, "token": res["token"], "user": res["user"]})


@app.post("/confer/tree")
async def confer_tree(request: Request) -> JSONResponse:
    if not _confer_user(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(db.confer_tree())


@app.post("/confer/history")
async def confer_history(request: Request) -> JSONResponse:
    if not _confer_user(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _, body = await _read(request)
    chat_id = int(body.get("chat_id") or 0)
    after = body.get("after_id")
    msgs = db.confer_list_messages(chat_id, after_id=int(after) if after else None)
    return JSONResponse({"messages": msgs})


@app.post("/confer/send")
async def confer_send(request: Request) -> JSONResponse:
    user = _confer_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _, body = await _read(request)
    return await _confer_store_and_fanout(
        body.get("chat_id"), user["username"], user["display_name"],
        body.get("kind"), body.get("text"), body.get("image"))


@app.post("/confer/subscriptions")
async def confer_subscriptions(request: Request) -> JSONResponse:
    user = _confer_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _, body = await _read(request)
    if body.get("action") == "set":
        ttype = "folder" if body.get("target_type") == "folder" else "chat"
        db.confer_set_subscription(user["id"], ttype, int(body.get("target_id") or 0), bool(body.get("on")))
    return JSONResponse({"subscriptions": db.confer_list_subscriptions(user["id"])})


@app.post("/confer/read")
async def confer_read(request: Request) -> JSONResponse:
    user = _confer_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _, body = await _read(request)
    db.confer_set_read(user["id"], int(body.get("chat_id") or 0), int(body.get("last_msg_id") or 0))
    return JSONResponse({"ok": True})


@app.post("/confer/folder")
async def confer_participant_folder(request: Request) -> JSONResponse:
    # Any authenticated participant may add to the shared tree (communal server). Deletion is
    # admin-only (see /confer/admin/folder) so one user can't wipe everyone's chats.
    if not _confer_user(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _, body = await _read(request)
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "Folder name required"}, status_code=400)
    parent = body.get("parent_id")
    db.confer_create_folder(name, int(parent) if parent else None)
    return JSONResponse(db.confer_tree())


@app.post("/confer/chat")
async def confer_participant_chat(request: Request) -> JSONResponse:
    if not _confer_user(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _, body = await _read(request)
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "Chat name required"}, status_code=400)
    folder = body.get("folder_id")
    db.confer_create_chat(name, int(folder) if folder else None)
    return JSONResponse(db.confer_tree())


# ---- admin endpoints (master/dashboard session): manage users + tree, send as 'admin' ----
@app.post("/confer/admin/users")
async def confer_admin_users(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    action = body.get("action")
    if action == "create":
        username = (body.get("username") or "").strip()
        password = (body.get("password") or "").strip()
        if len(username) < 2 or len(password) < 4:
            return JSONResponse({"error": "Username ≥2 and password ≥4 characters"}, status_code=400)
        uid = db.confer_create_user(username, password, (body.get("display_name") or "").strip())
        if not uid:
            return JSONResponse({"error": "Username already taken"}, status_code=409)
    elif action == "revoke":
        db.confer_revoke_user(int(body.get("user_id")), bool(body.get("revoked", True)))
    elif action == "reset":
        pw = (body.get("password") or "").strip()
        if len(pw) < 4:
            return JSONResponse({"error": "Password ≥4 characters"}, status_code=400)
        db.confer_set_password(int(body.get("user_id")), pw)
    return JSONResponse({"users": db.confer_list_users()})


@app.post("/confer/admin/folder")
async def confer_admin_folder(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if body.get("action") == "create":
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "Folder name required"}, status_code=400)
        parent = body.get("parent_id")
        db.confer_create_folder(name, int(parent) if parent else None)
    elif body.get("action") == "delete":
        db.confer_delete_folder(int(body.get("folder_id")))
    return JSONResponse(db.confer_tree())


@app.post("/confer/admin/chat")
async def confer_admin_chat(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if body.get("action") == "create":
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "Chat name required"}, status_code=400)
        folder = body.get("folder_id")
        db.confer_create_chat(name, int(folder) if folder else None)
    elif body.get("action") == "delete":
        db.confer_delete_chat(int(body.get("chat_id")))
    return JSONResponse(db.confer_tree())


@app.post("/confer/admin/tree")
async def confer_admin_tree(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({**db.confer_tree(), "presence": confer_hub.presence()})


@app.post("/confer/admin/history")
async def confer_admin_history(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    chat_id = int(body.get("chat_id") or 0)
    after = body.get("after_id")
    return JSONResponse({"messages": db.confer_list_messages(chat_id, after_id=int(after) if after else None)})


@app.post("/confer/admin/send")
async def confer_admin_send(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await _confer_store_and_fanout(
        body.get("chat_id"), "admin", "Admin", body.get("kind"), body.get("text"), body.get("image"))


# ---------------------------------------------------------------------------
# WebAuthn passkeys (fingerprint / Touch ID / Windows Hello)
# ---------------------------------------------------------------------------
@app.post("/webauthn/register/begin")
async def webauthn_register_begin(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    rp_id, origin = _rp_origin(request)
    username = db.get_config("master_username", "admin")
    state, options = passkeys.register_begin(rp_id, origin, username)
    return JSONResponse({"state": state, "options": json.loads(options)})


@app.post("/webauthn/register/complete")
async def webauthn_register_complete(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        ok = passkeys.register_complete(body.get("state", ""), json.dumps(body.get("credential")),
                                        body.get("label", ""))
    except Exception as exc:
        log.warning("Passkey registration failed: %s", exc)
        return JSONResponse({"error": "Registration failed"}, status_code=400)
    if not ok:
        return JSONResponse({"error": "Registration expired, try again"}, status_code=400)
    log.info("Passkey registered from %s", _client_ip(request))
    return JSONResponse({"ok": True})


@app.post("/webauthn/login/begin")
async def webauthn_login_begin(request: Request) -> JSONResponse:
    if not _rate_ok("login", _client_ip(request), max_n=20, window=300):
        return JSONResponse({"error": "Too many attempts"}, status_code=429)
    rp_id, origin = _rp_origin(request)
    res = passkeys.login_begin(rp_id, origin)
    if not res:
        return JSONResponse({"error": "No passkeys registered"}, status_code=404)
    state, options = res
    return JSONResponse({"state": state, "options": json.loads(options)})


@app.post("/webauthn/login/complete")
async def webauthn_login_complete(request: Request) -> JSONResponse:
    ip = _client_ip(request)
    if not _rate_ok("login", ip, max_n=20, window=300):
        return JSONResponse({"ok": False, "error": "Too many attempts"}, status_code=429)
    _, body = await _read(request)
    try:
        ok = passkeys.login_complete(body.get("state", ""), json.dumps(body.get("credential")))
    except Exception as exc:
        log.warning("Passkey login failed from %s: %s", ip, exc)
        ok = False
    if not ok:
        return JSONResponse({"ok": False, "error": "Passkey authentication failed"}, status_code=401)
    log.info("Passkey login from %s", ip)
    return JSONResponse({"ok": True, "token": auth.mint_session("admin")})


@app.post("/webauthn/list")
async def webauthn_list(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"passkeys": db.list_credentials()})


@app.post("/webauthn/delete")
async def webauthn_delete(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"ok": db.delete_credential((body.get("credential_id") or "").strip())})


# ---------------------------------------------------------------------------
# Config (view/update; session-gated), the web Settings tab
# ---------------------------------------------------------------------------
@app.post("/config/get")
async def config_get(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(
        {
            "username": db.get_config("master_username", ""),
            "print_width": print_width(),
            "auto_print_min_sev": db.get_config("auto_print_min_sev", _DEF_MIN_SEV),
            "auto_print_max_per_min": auto_print_fuse(),
            "log_retention_days": retention_days(),
            "err_retention_days": err_retention_days(),
            "disk_alert_pct": disk_alert_pct(),
            "notify": notifier.get_settings(),
            "mqtt": mqtt_bridge.get_settings(),
            "mqtt_client": mqtt_client.get_settings(),
        }
    )


@app.post("/config/set")
async def config_set(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if body.get("print_width") is not None:
        db.set_config("print_width", int(body["print_width"]))
    if body.get("auto_print_min_sev"):
        db.set_config("auto_print_min_sev", str(body["auto_print_min_sev"]))
    if body.get("auto_print_max_per_min") is not None:
        db.set_config("auto_print_max_per_min", int(body["auto_print_max_per_min"]))
    if body.get("log_retention_days") is not None:
        db.set_config("log_retention_days", int(body["log_retention_days"]))
    if body.get("err_retention_days") is not None:
        db.set_config("err_retention_days", int(body["err_retention_days"]))
    if body.get("disk_alert_pct") is not None:
        db.set_config("disk_alert_pct", int(body["disk_alert_pct"]))
    if isinstance(body.get("notify"), dict):
        notifier.save_settings(body["notify"])
    if isinstance(body.get("mqtt"), dict):
        mqtt_bridge.save_settings(body["mqtt"])
        await mqtt_bridge.reload()
    if isinstance(body.get("mqtt_client"), dict):
        mqtt_client.save_settings(body["mqtt_client"])
        await mqtt_client.reload()
    # Changing credentials requires a valid session (already checked above).
    new_user = (body.get("new_master_username") or "").strip()
    if new_user:
        auth.set_username(new_user)
        log.info("Master username changed via Settings from %s", _client_ip(request))
    new_pw = (body.get("new_master_password") or "").strip()
    if new_pw:
        if len(new_pw) < 4:
            return JSONResponse({"error": "Password too short"}, status_code=400)
        auth.set_password(new_pw)
        log.info("Master password changed via Settings from %s", _client_ip(request))
    return JSONResponse({"ok": True})


@app.post("/config/test-email")
async def config_test_email(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ok, msg = await asyncio.to_thread(
        notifier.send, "[Watchtower] test email", "This is a test from Watchtower notifications.")
    return JSONResponse({"ok": ok, "message": msg})


# ---------------------------------------------------------------------------
# Self-update, pull the latest main and restart, from the Settings tab.
# Needs a process supervisor to come back up: run under systemd (Restart=always) or Docker
# (restart: unless-stopped). Set UPDATE_RESTART_CMD to override the restart (e.g.
# "sudo systemctl restart watchtower"); otherwise the process re-execs itself in place.
# ---------------------------------------------------------------------------
def _repo_root() -> str | None:
    try:
        p = subprocess.run(["git", "-C", _SERVER_DIR, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


def _run_update() -> dict:
    root = _repo_root()
    if not root:
        return {"ok": False, "changed": False, "restarting": False,
                "log": "Not a git checkout, self-update is unavailable here."}
    lines: list[str] = []

    def run(cmd, timeout=300):
        try:
            p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
            lines.append(f"$ {' '.join(cmd)}\n{(p.stdout + p.stderr).strip()}")
            return p.returncode
        except Exception as exc:
            lines.append(f"$ {' '.join(cmd)}\n[error] {exc}")
            return 1

    before = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    rc = run(["git", "pull", "--ff-only", "origin", "main"], timeout=120)
    after = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    changed = before != after
    if changed:
        run([sys.executable, "-m", "pip", "install", "-q", "-r",
             os.path.join(_SERVER_DIR, "requirements.txt")])
    ok = rc == 0
    return {"ok": ok, "changed": changed, "before": before, "after": after,
            "restarting": ok and changed, "log": "\n\n".join(lines)}


def _deploy_payload(before: str, after: str) -> dict:
    root = _repo_root() or _SERVER_DIR
    commits = subprocess.run(
        ["git", "-C", root, "log", "--oneline", "--no-decorate", f"{before}..{after}"],
        capture_output=True, text=True).stdout.strip().splitlines()
    rule = "/" * 20
    L = [rule, "  DEPLOY", "  " + time.strftime("%Y-%m-%d %H:%M"),
         _kv("from", before), _kv("to", after), _kv("commits", str(len(commits))), rule, ""]
    L += ["> " + c for c in commits[:20]]
    if len(commits) > 20:
        L.append(f"  +{len(commits) - 20} more")
    L.append(rule)
    # ponytail: printed just before restart, best-effort. An offline printer misses it (the
    # queue is in memory). Persist to disk and replay on boot if that ever matters.
    return {"format": "plain", "text": "\n".join(L), "text_size": 26, "print_mode": "receipt"}


def _restart_process() -> None:
    cmd = os.environ.get("UPDATE_RESTART_CMD")
    try:
        if cmd:
            log.info("Restart via UPDATE_RESTART_CMD")
            subprocess.Popen(cmd, shell=True)
            return
        if "--reload" in sys.argv:
            # Under uvicorn --reload the socket is owned by a separate reloader process, so
            # re-exec'ing would spawn a second reloader and fail to bind (Address already in
            # use). Exit instead and let the supervisor relaunch us. REQUIRES a supervisor that
            # restarts on exit (systemd Restart=always), ideally drop the dev-only --reload flag.
            log.warning("Restart under --reload: exiting for the supervisor to relaunch. "
                        "Remove --reload and set Restart=always for clean restarts.")
            os._exit(3)
        log.info("Restart in place (os.execv)")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        log.error("Restart failed: %s", exc)


@app.post("/config/restart")
async def config_restart(request: Request) -> JSONResponse:
    """Restart the service without pulling, same restart path as self-update, no git/pip."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    log.info("Manual restart requested from %s", _client_ip(request))
    await relay.close_all()
    threading.Timer(1.0, _restart_process).start()
    return JSONResponse({"ok": True, "restarting": True})


_UPDATE_LOG = os.path.join(DATA_DIR, "update.log")


def _write_update_log(result: dict) -> None:
    # Persisted so the git pull output survives the restart and the dashboard can show it.
    head = (f"# {time.strftime('%Y-%m-%d %H:%M:%S')}  {result.get('before')} -> "
            f"{result.get('after')}  changed={result.get('changed')} ok={result.get('ok')}\n\n")
    try:
        with open(_UPDATE_LOG, "w") as f:
            f.write(head + (result.get("log") or ""))
    except Exception as exc:
        log.warning("Write update log failed: %s", exc)


async def _update_and_restart() -> None:
    result = await asyncio.to_thread(_run_update)
    _write_update_log(result)
    log.info("Self-update: changed=%s %s->%s ok=%s",
             result.get("changed"), result.get("before"), result.get("after"), result.get("ok"))
    if not result.get("restarting"):
        return
    try:
        await _render_and_submit(_deploy_payload(result["before"], result["after"]))
    except Exception as exc:
        log.warning("Deploy receipt failed: %s", exc)
    await relay.close_all()
    threading.Timer(1.5, _restart_process).start()


@app.post("/config/update")
async def config_update(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Run the slow pull + pip in the background and respond at once, so a long update never trips
    # the reverse proxy timeout (502). The client polls /healthz and reloads when the server is back.
    asyncio.create_task(_update_and_restart())
    return JSONResponse({"ok": True, "restarting": True, "started": True})


@app.post("/config/update-log")
async def config_update_log(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        with open(_UPDATE_LOG) as f:
            return JSONResponse({"log": f.read()})
    except FileNotFoundError:
        return JSONResponse({"log": ""})


# ---------------------------------------------------------------------------
# Password check + rendering
# ---------------------------------------------------------------------------
@app.post("/check")
async def check(request: Request) -> JSONResponse:
    # Operator-only (session/master): don't expose a public password-probing oracle.
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    provided = (body.get("target") or "").strip()
    if auth.is_master(provided):
        return JSONResponse({"valid": True, "unlimited": True, "message": "No usage limit"})
    row = db.find_temp_password(provided)
    if row and not row["revoked"] and (row["max_uses"] - row["used"]) > 0:
        n = row["max_uses"] - row["used"]
        return JSONResponse({"valid": True, "unlimited": False, "remaining": n,
                             "message": f"{n} usage{'s' if n != 1 else ''} left"})
    return JSONResponse({"valid": False, "message": "Invalid password"})


@app.post("/preview")
async def preview(request: Request) -> Response:
    # Operator (session/master) OR a valid temp password (the public print page).
    _, payload = await _read(request)
    admin = _authed_admin(request, payload)
    if not (admin or _valid_temp_password(payload.get("password"))):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # STATUS and brief render internals, operator only, and build their own content.
    if payload.get("format") in ("status", "brief") and not admin:
        return JSONResponse({"error": "Not available on public prints"}, status_code=403)
    if payload.get("format") == "brief":
        return Response(content=rendermod.to_png_bytes(await _build_brief_image()), media_type="image/png")
    if payload.get("format") == "status":
        payload = _status_payload(str(payload.get("by") or "dashboard preview"))
    if payload.get("format") == "battery":
        payload = _battery_sample_payload()
    if not admin:
        err = _public_limit_error(payload)
        if err:
            return JSONResponse({"error": err}, status_code=400)
    try:
        img = rendermod.render(payload, print_width())
    except rendermod.RenderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return Response(content=rendermod.to_png_bytes(img), media_type="image/png")


# ---------------------------------------------------------------------------
# Print / alert intake
# ---------------------------------------------------------------------------
@app.post("/print")
async def do_print(request: Request) -> JSONResponse:
    raw, payload = await _read(request)
    return await _print_payload(payload, authed_device=_hmac_device(request, raw),
                                authed_session=_session_ok(request))


@app.post("/alert")
async def do_alert(request: Request) -> JSONResponse:
    raw, body = await _read(request)
    payload = {
        "password": body.get("password") or body.get("code"),
        "format": "alert",
        "alert_type": body.get("alert_type") or body.get("type") or "alert",
        "text": body.get("message") or body.get("text") or "",
        "service": body.get("service") or "",
        "sent_at": body.get("sent_at") or body.get("timestamp"),
        "print_mode": body.get("print_mode", "receipt"),
    }
    return await _print_payload(payload, authed_device=_hmac_device(request, raw),
                                authed_session=_session_ok(request))


async def _render_and_submit(payload: dict, on_delivered=None) -> bool:
    """Render to exact pixels and push to the device (trusted HMAC channel, no per-job password)."""
    if payload.get("format") == "brief":
        img = await _build_brief_image()
        job = {"format": "image", "print_mode": "receipt", "image_raw_bitmap": rendermod.to_base64_png(img)}
        return await relay.submit(job, on_delivered=on_delivered)
    if payload.get("format") == "status":
        payload = _status_payload(str(payload.get("by") or payload.get("service") or ""))
    img = rendermod.render(payload, print_width())
    job = {
        "format": "image",
        "print_mode": payload.get("print_mode", "receipt"),
        "image_raw_bitmap": rendermod.to_base64_png(img),
    }
    return await relay.submit(job, on_delivered=on_delivered)


async def _mqtt_on_message(kind: str, payload: dict) -> None:
    """A print/alert arrived on the Watchtower MQTT broker (already authenticated), render it
    and relay to the printer, same as a manual/error print."""
    if kind == "alert":
        p = _log_to_alert_payload(
            str(payload.get("device") or "mqtt"),
            str(payload.get("alert_type") or payload.get("type") or "alert"),
            str(payload.get("service") or ""),
            str(payload.get("message") or payload.get("text") or ""),
            payload.get("ts") or time.time(),
        )
    else:
        p = dict(payload)
        p.pop("password", None)
        p.pop("code", None)
    try:
        delivered = await _render_and_submit(p)
    except rendermod.RenderError as exc:
        log.warning("MQTT render failed: %s", exc)
        return
    db.add_history({"format": p.get("format", "?"), "label": _label_for(p), "user": "mqtt",
                    "status": "printed" if delivered else "queued"})
    log.info("MQTT %s relayed to printer (delivered=%s)", kind, delivered)


mqtt_bridge.on_message = _mqtt_on_message
mqtt_client.on_message = _mqtt_on_message


async def _print_payload(payload: dict, authed_device=None, authed_session=False) -> JSONResponse:
    provided = (payload.get("password") or payload.get("code") or "").strip()

    if authed_device or authed_session:
        user, unlimited, temp_pw = (authed_device or "operator"), True, None
    elif auth.is_master(provided):
        user, unlimited, temp_pw = "master", True, None
    else:
        row = db.find_temp_password(provided)
        if not row or row["revoked"] or (row["max_uses"] - row["used"]) <= 0:
            return JSONResponse({"error": "Invalid password or no usages left"}, status_code=401)
        user, unlimited, temp_pw = row["user"], False, provided

    # STATUS and the daily brief reveal internals, never allow them on a public (temp-password) print.
    if payload.get("format") in ("status", "brief") and temp_pw:
        return JSONResponse({"error": "Not available on public prints"}, status_code=403)
    if payload.get("format") == "status":
        payload = _status_payload(str(payload.get("by") or payload.get("service") or user))
    if payload.get("format") == "battery":
        payload = _battery_sample_payload()

    # Temp-password (public) prints are size-capped and image-free to protect the paper roll.
    if temp_pw:
        err = _public_limit_error(payload)
        if err:
            return JSONResponse({"error": err}, status_code=400)

    try:
        img = await _build_brief_image() if payload.get("format") == "brief" \
            else rendermod.render(payload, print_width())
    except rendermod.RenderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    job = {
        "format": "image",
        "print_mode": payload.get("print_mode", "receipt"),
        "image_raw_bitmap": rendermod.to_base64_png(img),
    }
    fmt = payload.get("format", "?")
    label = _label_for(payload)
    consumed: dict = {}

    def on_delivered() -> None:
        if temp_pw and db.consume_temp_password(temp_pw):
            row2 = db.find_temp_password(temp_pw)
            consumed["remaining"] = (row2["max_uses"] - row2["used"]) if row2 else 0
        db.add_history({"format": fmt, "label": label, "user": user, "status": "printed"})
        log.info("Printed [%s] '%s' for %s", fmt, label, user)

    delivered = await relay.submit(job, on_delivered=on_delivered)

    if delivered:
        remaining = consumed.get("remaining")
        return JSONResponse(
            {"ok": True, "delivered": True, "queued": False, "unlimited": unlimited,
             "remaining": remaining, "usage_message": _usage_message(unlimited, remaining),
             "message": "Sent to device"}
        )

    db.add_history({"format": fmt, "label": label, "user": user, "status": "queued"})
    log.info("Queued [%s] '%s' for %s (no device connected)", fmt, label, user)
    return JSONResponse(
        {"ok": True, "delivered": False, "queued": True, "unlimited": unlimited,
         "remaining": None, "usage_message": _usage_message(unlimited, None),
         "message": "No device connected, job queued (a temp use counts only when it prints)"}
    )


# ---------------------------------------------------------------------------
# Watchtower, log ingestion (HMAC only)
# ---------------------------------------------------------------------------
def _auto_print_allowed() -> bool:
    fuse = auto_print_fuse()
    if fuse <= 0:
        return True
    now = time.time()
    while _auto_print_times and now - _auto_print_times[0] > 60:
        _auto_print_times.popleft()
    return len(_auto_print_times) < fuse


def _log_to_alert_payload(device_id: str, severity: str, service: str, message: str, ts) -> dict:
    return {
        "format": "alert",
        "alert_type": severity,
        "text": message,
        "service": f"{device_id}" + (f" / {service}" if service else ""),
        "sent_at": ts,
        "print_mode": "receipt",
    }


# Severity overrides: operator rules that lower the severity of matching (noisy) log messages at
# ingest, so a service that cries `err` over nothing stops printing and alerting. Managed in
# Settings; created from a log in the Logs tab. Stored as JSON in config (small, rarely changes).
def _load_overrides() -> list:
    try:
        return json.loads(db.get_config("severity_overrides", "[]")) or []
    except (ValueError, TypeError):
        return []


def _save_overrides(rules: list) -> None:
    db.set_config("severity_overrides", json.dumps(rules))


def _apply_overrides(service: str, message: str, severity: str) -> str:
    """Lower `severity` to a rule's target when its (optional) service matches and its substring is
    in the message. Only ever lowers, never raises."""
    for r in _load_overrides():
        svc = (r.get("service") or "").strip()
        if svc and svc != service:
            continue
        match = r.get("match") or ""
        if match and match not in message:
            continue
        target = r.get("severity")
        if target in SEVERITY_NAMES and sev_num(target) > sev_num(severity):
            severity = target
    return severity


SEVERITY_NAMES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")


# Low-battery alerts for a printer. Each threshold fires once as the level crosses it going down,
# then re-arms when the battery recovers back above it (charging / battery swap), so a printer
# sitting at a low level doesn't re-alert every status frame.
BATTERY_THRESHOLDS = (20, 10, 5)
_BATT_SEV = {20: "warning", 10: "err", 5: "crit"}


def _battery_message(label: str, level: int, threshold: int) -> str:
    return f"{label} battery low: {level}% (alert at {threshold}%)"


def _battery_updates(pm: dict, label: str) -> list:
    """Mutate pm['batt_alerted'] for the printer's current level; return alerts to fire (at most the
    most severe threshold just crossed, so a big drop doesn't spew a stack of prints)."""
    try:
        level = int(pm.get("battery"))
    except (TypeError, ValueError):
        return []
    alerted = set(pm.get("batt_alerted") or [])
    crossed = []
    for t in BATTERY_THRESHOLDS:
        if level <= t and t not in alerted:
            alerted.add(t)
            crossed.append(t)
        elif level > t and t in alerted:
            alerted.discard(t)  # recovered above this threshold, re-arm it
    pm["batt_alerted"] = sorted(alerted, reverse=True)
    if not crossed:
        return []
    t = min(crossed)  # smallest threshold crossed == most severe
    return [(_BATT_SEV.get(t, "warning"), "printer.battery", _battery_message(label, level, t))]


def _battery_sample_payload() -> dict:
    """A representative low-battery alert for the Print tab, so the printed form can be tested
    without draining a battery. Uses the connected printer's id if there is one."""
    printer = next((d for d in db.list_devices()
                    if not d["revoked"] and (d.get("meta") or {}).get("role") == "printer"), None)
    label = (printer or {}).get("name") or (printer or {}).get("id") or "printer"
    return _log_to_alert_payload(label, "crit", "printer.battery",
                                 _battery_message(label, 5, 5), time.time())


# ---------------------------------------------------------------------------
# STATUS, a printed system report (printer + host + watchdog + every scout).
# The design carries the "terminal readout" feel; the content is all live data.
# ---------------------------------------------------------------------------
def _dur(secs: float) -> str:
    s = int(secs)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


def _ago(ts) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    for unit, n in (("s", 60), ("m", 60), ("h", 24)):
        if s < n:
            return f"{s}{unit} ago"
        s //= n
    return f"{s}d ago"


def _kv(label: str, value) -> str:
    return f" {label:<6}: {value}"


def _host_lines() -> "list[str]":
    out = []
    try:
        out.append(_kv("node", socket.gethostname()))
    except Exception:
        pass
    try:
        out.append(_kv("os", f"{platform.system()} {platform.release()}"))
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            out.append(_kv("up", _dur(float(f.read().split()[0]))))
    except Exception:
        pass
    try:
        la = os.getloadavg()
        out.append(_kv("load", f"{la[0]:.2f} {la[1]:.2f} {la[2]:.2f}"))
    except Exception:
        pass
    try:
        out.append(_kv("cpu", f"{os.cpu_count()} cores"))
    except Exception:
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0])
        total, avail = info.get("MemTotal", 0) // 1024, info.get("MemAvailable", 0) // 1024
        if total:
            out.append(_kv("mem", f"{total - avail}/{total} MB"))
    except Exception:
        pass
    try:
        du = shutil.disk_usage("/")
        out.append(_kv("disk", f"{du.used // 10**9}/{du.total // 10**9} GB {du.used * 100 // du.total}%"))
    except Exception:
        pass
    try:
        rss = "?"
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    rss = f"{int(line.split()[1]) // 1024} MB"
                    break
        out.append(_kv("proc", f"pid {os.getpid()}"))
        out.append(_kv("rss", rss))
    except Exception:
        pass
    return out


def _scout_block(d: dict, counts: dict, now: float) -> "list[str]":
    """A clearly-delimited block per scout so they're easy to tell apart."""
    meta = d.get("meta") or {}
    hb = d.get("heartbeat_secs") or 0
    onl = agents.online(d["id"]) or bool(d["last_seen_at"] and now - d["last_seen_at"] < max(90, hb * 2))
    m = meta.get("metrics") or {}
    c = counts.get(d["id"], {})
    crit = sum(c.get(s, 0) for s in ("emerg", "alert", "crit"))
    err = c.get("err", 0)
    warn = c.get("warning", 0)
    bar = "#" * 20
    L = [bar, f" {d['id']}", bar,
         _kv("name", d.get("name") or "-"),
         _kv("state", "UP" if onl else "DOWN"),
         _kv("ver", meta.get("scout_version", "?")),
         _kv("host", meta.get("host", "?")),
         _kv("added", time.strftime("%Y-%m-%d", time.localtime(d["created_at"])) if d.get("created_at") else "?"),
         _kv("seen", _ago(d["last_seen_at"])),
         _kv("hb", f"{hb}s" if hb else "off")]
    if m.get("load1") is not None:
        L.append(_kv("load", f"{m['load1']} ({m.get('cpus', '?')}cpu)"))
    if m.get("mem_pct") is not None:
        L.append(_kv("mem", f"{m['mem_pct']}%"))
    if m.get("disk_pct") is not None:
        L.append(_kv("disk", f"{m['disk_pct']}%"))
    if m.get("temp_c") is not None:
        L.append(_kv("temp", f"{m['temp_c']}C"))
    L.append(_kv("logs24", f"c{crit} e{err} w{warn}"))
    L.append("")
    return L


def _build_status_report(by: str) -> str:
    now = time.time()
    rule = "/" * 20
    L = [rule, "      STATUS",
         "  " + time.strftime("%Y-%m-%d %H:%M"),
         f"  by: {by or 'unknown'}", rule, ""]

    devices = db.list_devices()
    active = [d for d in devices if not d["revoked"]]
    printer_dev = next((d for d in active if (d.get("meta") or {}).get("role") == "printer"), None)
    scouts = [d for d in active if d is not printer_dev]

    # Printer
    link = "CONFER" if relay.any_confer() else ("ONLINE" if relay.is_connected() else "OFFLINE")
    L += ["== PRINTER ==",
          _kv("link", link),
          _kv("mode", "confer" if relay.any_confer() else "print"),
          _kv("queue", f"{sum(len(q) for q in relay.pending.values())} jobs"),
          _kv("width", f"{print_width()}px")]
    pm = (printer_dev or {}).get("meta") or {}
    if relay.is_connected():
        L.append(_kv("state", "ready" if pm.get("ready", True) else f"NOT READY ({pm.get('printer_state', '?')})"))
    if pm.get("battery") is not None:
        L.append(_kv("battery", f"{pm['battery']}% {'charging' if pm.get('charging') else 'on battery'}"))
    if pm.get("serial"):
        L.append(_kv("serial", pm["serial"]))
    L.append("")

    # Host
    L += ["== HOST =="] + _host_lines() + [""]

    # Watchdog (scouts only, the printer is not a dead-man's-switch target)
    armed = [d for d in scouts if (d.get("heartbeat_secs") or 0) > 0]
    silent = [d["id"] for d in scouts if d["id"] in _silent_devices]
    L += ["== WATCHDOG ==",
          _kv("scouts", f"{len(scouts)} ({len(devices) - len(active)} revoked)"),
          _kv("armed", f"{len(armed)}"),
          _kv("silent", ", ".join(silent) if silent else "none"),
          _kv("check", "30s"), ""]

    # Scouts, one clearly-boxed block each
    counts = db.severity_counts()
    L.append(f"== SCOUTS ({len(scouts)}) ==")
    L.append("")
    if not scouts:
        L.append(" none paired")
    for d in sorted(scouts, key=lambda x: x["id"]):
        L += _scout_block(d, counts, now)
    L.append(rule)
    return "\n".join(L)


def _status_payload(by: str) -> dict:
    # A touch denser than the global default so the data report stays legible on the roll.
    return {"format": "plain", "text": _build_status_report(by),
            "print_mode": "receipt", "text_size": 26}


# Daily brief: weather (Open-Meteo, keyless) + a condensed systems summary.
_BUCHAREST = "latitude=44.4268&longitude=26.1025"
_weather_cache = {"ts": 0.0, "data": {}}


def _fetch_weather() -> dict:
    if _weather_cache["data"] and time.time() - _weather_cache["ts"] < 600:
        return _weather_cache["data"]
    url = ("https://api.open-meteo.com/v1/forecast?" + _BUCHAREST +
           "&current=temperature_2m,apparent_temperature,weather_code"
           "&hourly=temperature_2m,precipitation_probability"
           "&minutely_15=temperature_2m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
           "precipitation_probability_max,uv_index_max,wind_speed_10m_max"
           "&timezone=Europe%2FBucharest&forecast_days=4")
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode())
    # Air quality is a separate keyless endpoint; a failure here must not break the weather.
    air_url = ("https://air-quality-api.open-meteo.com/v1/air-quality?" + _BUCHAREST +
               "&current=european_aqi,pm2_5,pm10&timezone=Europe%2FBucharest")
    try:
        with urllib.request.urlopen(air_url, timeout=10) as r:
            data["air_quality"] = json.loads(r.read().decode()).get("current", {})
    except Exception as exc:
        log.warning("Air quality fetch failed: %s", exc)
    _weather_cache.update(ts=time.time(), data=data)
    return data


def _printer_meta() -> dict:
    for d in db.list_devices():
        if not d["revoked"] and (d.get("meta") or {}).get("role") == "printer":
            return d.get("meta") or {}
    return {}


def _server_brief_lines() -> "list[str]":
    now = time.time()
    devices = db.list_devices()
    active = [d for d in devices if not d["revoked"]]
    printer = next((d for d in active if (d.get("meta") or {}).get("role") == "printer"), None)
    scouts = [d for d in active if d is not printer]
    pm = (printer or {}).get("meta") or {}
    plink = "confer" if relay.any_confer() else ("online" if relay.is_connected() else "OFFLINE")
    batt = f", batt {pm['battery']}%" if pm.get("battery") is not None else ""
    up = sum(1 for d in scouts if agents.online(d["id"]) or
             (d["last_seen_at"] and now - d["last_seen_at"] < max(90, (d.get("heartbeat_secs") or 0) * 2)))
    counts = db.severity_counts()
    errs = sum(sum(c.get(s, 0) for s in ("emerg", "alert", "crit", "err")) for c in counts.values())
    lines = [f"> printer: {plink}{batt}",
             f"> scouts:  {up}/{len(scouts)} up",
             f"> 24h err: {errs}"]
    silent = [d["id"] for d in scouts if d["id"] in _silent_devices]
    if silent:
        lines.append(f"> silent:  {', '.join(silent)}")
    return lines


async def _build_brief_image():
    try:
        weather = await asyncio.to_thread(_fetch_weather)
    except Exception as exc:
        log.warning("Weather fetch failed: %s", exc)
        weather = {}
    return rendermod.render_brief(weather, _server_brief_lines(), "GOOD MORNING")


# Dedup so a flapping condition doesn't spam email/print repeatedly.
_recent_alerts: dict[str, float] = {}
# Devices flagged silent by the dead-man's-switch (alert once, not every check).
_silent_devices: set[str] = set()


def _dedup_ok(key: str, window: float = 300.0) -> bool:
    now = time.time()
    if now - _recent_alerts.get(key, 0.0) < window:
        return False
    _recent_alerts[key] = now
    return True


async def _maybe_email(device_id: str, severity: str, service: str, message: str) -> None:
    """Email the operator if notifications are on and severity meets the floor (deduped)."""
    st = notifier.get_settings()
    if not st["enabled"] or sev_num(severity) > sev_num(st["min_sev"]):
        return
    if not _dedup_ok(f"email:{device_id}:{service}:{message}"):
        return
    subject = f"[Watchtower] {severity.upper()} {device_id}" + (f"/{service}" if service else "")
    ok, err = await asyncio.to_thread(notifier.send, subject, f"{device_id}/{service}\n\n{message}")
    if not ok:
        log.warning("Email notify failed: %s", err)


@app.post("/agent/poll")
async def agent_poll(request: Request) -> JSONResponse:
    """Scout agent heartbeat + command channel (HMAC). Held open until a command is queued
    for this device, or ~25s. The poll keeps the device marked online."""
    raw, body = await _read(request)
    device_id = _hmac_device(request, raw)
    if not device_id:
        return JSONResponse({"error": "Unauthorized (HMAC required)"}, status_code=401)
    meta = {}
    if body.get("version"):
        meta["scout_version"] = str(body["version"])
    if body.get("host"):
        meta["host"] = str(body["host"])
    metrics = body.get("metrics") if isinstance(body.get("metrics"), dict) else None
    if metrics:
        meta["metrics"] = metrics  # cpu/mem/disk/temp, shown on the device card
    if isinstance(body.get("cameras"), list):
        meta["cameras"] = body["cameras"]  # webcams the scout can stream
    if isinstance(body.get("proxmox"), dict):
        meta["proxmox"] = body["proxmox"]  # VM/LXC inventory on a Proxmox node
        _proxmox_devices.add(device_id)    # mark it for error-burst coalescing
    if meta:
        # Merge, don't replace: keep operator-set keys (e.g. cameras_selected) the poll never sends.
        merged = db.device_meta(device_id)
        merged.update(meta)
        db.touch_device(device_id, meta=merged)
    # Disk-full alert (print + email), deduped.
    if metrics and disk_alert_pct() > 0:
        try:
            disk = float(metrics.get("disk_pct"))
        except (TypeError, ValueError):
            disk = -1
        if disk >= disk_alert_pct() and _dedup_ok(f"disk:{device_id}", window=1800):
            msg = f"disk at {disk:.0f}% (threshold {disk_alert_pct()}%)"
            await _fire_alert(device_id, "crit", "host.disk", msg)
    cmd = await agents.wait(device_id, timeout=25.0)
    return JSONResponse({"cmd": cmd})


async def _fire_alert(device_id: str, severity: str, service: str, message: str) -> None:
    """Print (respecting the fuse) AND email an internally-generated alert (silence, disk, …)."""
    try:
        if _auto_print_allowed():
            payload = _log_to_alert_payload(device_id, severity, service, message, time.time())
            if await _render_and_submit(payload):
                _auto_print_times.append(time.time())
    except Exception as exc:
        log.error("Alert print failed (%s/%s): %s", device_id, service, exc)
    db.add_log(device_id=device_id, severity=severity, message=message, service=service,
               meta={"generated": True}, source_ip="watchtower")
    await _maybe_email(device_id, severity, service, message)


# ---------------------------------------------------------------------------
# Proxmox error-burst coalescer. A node forwards a whole fleet's logs, so a fault can dump hundreds
# of errors at once. For Proxmox scouts only, a burst suppresses the individual prints and emits one
# "multiple errors" summary (per-source counts) instead. Every line still lands in the dashboard.
# ---------------------------------------------------------------------------
_proxmox_devices: set[str] = set()
_BURST_WINDOW = 10.0        # seconds of history used to judge the rate
_BURST_THRESHOLD = 20       # errors within the window == a burst
_BURST_SUMMARY_EVERY = 30.0  # while bursting, emit at most one summary this often
_BURST_QUIET = 8.0          # flush the final summary once errors stop for this long


class _Burst:
    def __init__(self) -> None:
        self.window: deque = deque()
        self.pending: dict[str, int] = {}
        self.pending_total = 0
        self.first_ts = 0.0
        self.last_err_ts = 0.0
        self.last_summary_ts = 0.0


_bursts: dict[str, _Burst] = {}
_flusher_task = None


def _burst_source(service: str) -> str:
    return service.split("/", 1)[0] if "/" in service else (service or "unknown")


def _note_error_burst(device_id: str, service: str) -> bool:
    """Record an error from a Proxmox scout; return True if it falls inside a burst (suppress it)."""
    now = time.time()
    b = _bursts.setdefault(device_id, _Burst())
    b.window.append(now)
    while b.window and now - b.window[0] > _BURST_WINDOW:
        b.window.popleft()
    b.last_err_ts = now
    if len(b.window) < _BURST_THRESHOLD:
        return False
    src = _burst_source(service)
    b.pending[src] = b.pending.get(src, 0) + 1
    b.pending_total += 1
    if not b.first_ts:
        b.first_ts = now
    return True


async def _emit_burst_summary(device_id: str, b: _Burst) -> None:
    if b.pending_total <= 0:
        return
    span = max(1, int(b.last_err_ts - b.first_ts))
    top = sorted(b.pending.items(), key=lambda kv: kv[1], reverse=True)[:6]
    who = ", ".join(f"{s} ({c})" for s, c in top)
    msg = f"multiple errors: {b.pending_total} in {span}s from {who}"
    b.pending = {}
    b.pending_total = 0
    b.first_ts = 0.0
    b.last_summary_ts = time.time()
    await _fire_alert(device_id, "err", "burst", msg)


async def _burst_flusher() -> None:
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for device_id, b in list(_bursts.items()):
            if b.pending_total > 0 and now - b.last_err_ts >= _BURST_QUIET:
                try:
                    await _emit_burst_summary(device_id, b)
                except Exception as exc:
                    log.error("Burst flush failed for %s: %s", device_id, exc)


def _ensure_flusher() -> None:
    global _flusher_task
    if _flusher_task is None:
        _flusher_task = asyncio.create_task(_burst_flusher())


@app.post("/ingest")
async def ingest(request: Request) -> JSONResponse:
    raw, body = await _read(request)
    device_id = _hmac_device(request, raw)
    if not device_id:
        return JSONResponse({"error": "Unauthorized (HMAC required)"}, status_code=401)

    severity = (body.get("severity") or body.get("level") or "info").lower()
    if severity not in ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"):
        severity = "info"
    message = str(body.get("message") or body.get("text") or "")
    service = str(body.get("service") or "")
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    # Forwarded logs / command output set no_print so they don't spew paper.
    no_print = bool(body.get("no_print") or body.get("auto_print") is False)
    severity = _apply_overrides(service, message, severity)  # operator noise-suppression rules

    # Proxmox burst coalescing: during a flood, suppress the individual print and let one summary
    # (emitted here or by the flusher) carry it instead. The line is still stored below.
    if device_id in _proxmox_devices and sev_num(severity) <= 3:
        _ensure_flusher()
        if _note_error_burst(device_id, service):
            no_print = True
            b = _bursts[device_id]
            if time.time() - b.last_summary_ts >= _BURST_SUMMARY_EVERY:
                await _emit_burst_summary(device_id, b)

    should_print = sev_num(severity) <= auto_print_max_num() and not no_print
    printed = False
    if should_print and _auto_print_allowed():
        try:
            payload = _log_to_alert_payload(device_id, severity, service, message, body.get("ts") or time.time())
            if await _render_and_submit(payload):
                printed = True
                _auto_print_times.append(time.time())
        except Exception as exc:
            log.error("Auto-print failed for %s/%s: %s", device_id, service, exc)
    elif should_print:
        log.warning("Auto-print fuse tripped (>%d/min), %s/%s not printed", auto_print_fuse(), device_id, service)

    log_id = db.add_log(device_id=device_id, severity=severity, message=message, service=service,
                        meta=meta, source_ip=_client_ip(request), printed=printed)
    await _maybe_email(device_id, severity, service, message)
    log.info("Ingest #%d [%s] %s/%s printed=%s", log_id, severity, device_id, service, printed)
    return JSONResponse({"ok": True, "id": log_id, "printed": printed, "would_print": should_print})


# ---------------------------------------------------------------------------
# Camera relay. A scout streams a webcam (ffmpeg MJPEG) up to /agent/camera/push; the browser
# reads it back as multipart/x-mixed-replace from /watchtower/camera/stream. The whole path rides
# the same TLS as everything else. A camera is captured only while at least one viewer is attached:
# when the last <img> closes, the push loop sees viewers==0 and returns, dropping the scout's
# connection so ffmpeg dies. Nothing streams when the tab is hidden or another camera is focused.
# ---------------------------------------------------------------------------
class CamChannel:
    def __init__(self) -> None:
        self.frame = b""
        self.seq = 0
        self.viewers = 0
        self.pushing = False
        self.start_ts = 0.0
        self.frame_ts = 0.0
        self.event = asyncio.Event()

    def set_frame(self, data: bytes) -> None:
        self.frame = data
        self.seq += 1
        self.frame_ts = time.time()
        ev, self.event = self.event, asyncio.Event()
        ev.set()


_cam_channels: dict[str, CamChannel] = {}
_cam_tokens: dict[str, tuple[str, str, float]] = {}  # push token -> (device, node, expiry)


def _channel(device: str, node: str) -> CamChannel:
    key = f"{device}|{node}"
    ch = _cam_channels.get(key)
    if ch is None:
        ch = _cam_channels[key] = CamChannel()
    return ch


def _ensure_pushing(device: str, node: str, ch: CamChannel) -> None:
    """Ask the scout to start capturing this camera, unless it already is (or was asked recently)."""
    if ch.pushing or time.time() - ch.start_ts < 8 or not agents.online(device):
        return
    token = secrets.token_urlsafe(24)
    _cam_tokens[token] = (device, node, time.time() + 30)
    ch.start_ts = time.time()
    agents.queue(device, {"cmd": "camera", "action": "start", "node": node,
                          "token": token, "fps": 10, "size": "640x480"})


async def _mjpeg(ch: CamChannel):
    last = -1
    while True:
        ev = ch.event
        if ch.seq != last and ch.frame:  # skip the empty initial frame; wait for a real one
            last = ch.seq
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                   % len(ch.frame)) + ch.frame + b"\r\n"
        else:
            try:
                await asyncio.wait_for(ev.wait(), 15)
            except asyncio.TimeoutError:
                if not ch.pushing:
                    break  # scout never connected or its stream died


@app.get("/watchtower/camera/stream")
async def camera_stream(request: Request):
    # Token in the query, since an <img> can't send an Authorization header (same pattern as Confer).
    if not auth.verify_session(request.query_params.get("token")):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    device = (request.query_params.get("device") or "").strip()
    node = (request.query_params.get("node") or "").strip()
    if not device or not node:
        return JSONResponse({"error": "device and node required"}, status_code=400)
    ch = _channel(device, node)

    async def gen():
        ch.viewers += 1
        _ensure_pushing(device, node, ch)
        try:
            async for part in _mjpeg(ch):
                yield part
        finally:
            ch.viewers -= 1

    # X-Accel-Buffering: no tells nginx not to buffer the feed, so frames reach the browser live.
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={**_NO_CACHE, "X-Accel-Buffering": "no"})


@app.get("/watchtower/camera/snapshot")
async def camera_snapshot(request: Request):
    """One still frame for the Cameras grid thumbnails: returns the last cached frame if recent,
    else briefly captures one (registers a transient viewer so the scout starts, waits for a fresh
    frame, then releases so capture stops). Keeps idle tiles showing a picture, not a blank."""
    if not auth.verify_session(request.query_params.get("token")):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    device = (request.query_params.get("device") or "").strip()
    node = (request.query_params.get("node") or "").strip()
    if not device or not node:
        return JSONResponse({"error": "device and node required"}, status_code=400)
    ch = _channel(device, node)
    if ch.frame and time.time() - ch.frame_ts < 120:
        return Response(content=ch.frame, media_type="image/jpeg", headers=_NO_CACHE)
    start = ch.seq
    ch.viewers += 1
    _ensure_pushing(device, node, ch)
    try:
        for _ in range(100):  # up to ~10s for the first fresh frame
            if ch.seq != start:
                break
            await asyncio.sleep(0.1)
    finally:
        ch.viewers -= 1
    if ch.frame:
        return Response(content=ch.frame, media_type="image/jpeg", headers=_NO_CACHE)
    return Response(status_code=503)


@app.post("/agent/camera/push")
async def camera_push(request: Request) -> Response:
    info = _cam_tokens.pop(request.query_params.get("token") or "", None)
    if not info or info[2] < time.time():
        return JSONResponse({"error": "bad or expired token"}, status_code=403)
    device, node, _ = info
    ch = _channel(device, node)
    ch.pushing = True
    log.info("Camera push connected: %s %s (%d viewer(s))", device, node, ch.viewers)
    frames = 0
    total = 0
    buf = bytearray()
    empty_since = 0.0
    try:
        async for chunk in request.stream():
            buf += chunk
            total += len(chunk)
            while True:  # split the raw MJPEG byte stream into JPEG frames (SOI..EOI)
                s = buf.find(b"\xff\xd8")
                if s < 0:
                    if len(buf) > 2_000_000:
                        del buf[:-2]
                    break
                e = buf.find(b"\xff\xd9", s + 2)
                if e < 0:
                    if s:
                        del buf[:s]
                    break
                ch.set_frame(bytes(buf[s:e + 2]))
                frames += 1
                del buf[:e + 2]
            if ch.viewers <= 0:
                if not empty_since:
                    empty_since = time.time()
                elif time.time() - empty_since > 2:
                    break  # nobody watching, return -> connection closes -> scout stops ffmpeg
            else:
                empty_since = 0.0
    except Exception:
        pass
    finally:
        ch.pushing = False
        log.info("Camera push ended: %s %s (%d bytes, %d frames relayed)", device, node, total, frames)
    return Response(status_code=204)


@app.post("/watchtower/camera/select")
async def camera_select(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    device = (body.get("device") or "").strip()
    node = (body.get("node") or "").strip()
    if not device or not node:
        return JSONResponse({"error": "device and node required"}, status_code=400)
    meta = db.device_meta(device)
    sel = [n for n in (meta.get("cameras_selected") or []) if isinstance(n, str) and n != node]
    if body.get("selected"):
        sel.append(node)
    meta["cameras_selected"] = sel
    db.set_device_meta(device, meta)
    return JSONResponse({"ok": True, "cameras_selected": sel})


# ---------------------------------------------------------------------------
# Watchtower, dashboard data (session or master password)
# ---------------------------------------------------------------------------
@app.post("/watchtower/logs")
async def watchtower_logs(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    max_sev = body.get("max_sev")
    logs = db.list_logs(
        limit=int(body.get("limit", 200)),
        before_id=body.get("before_id"),
        max_sev=sev_num(max_sev) if max_sev else None,
        device_id=body.get("device_id") or None,
        service=body.get("service") or None,
        search=(body.get("search") or "").strip() or None,
    )
    devices = db.list_devices()
    for d in devices:
        d["agent_online"] = agents.online(d["id"])
    return JSONResponse(
        {"logs": logs, "devices": devices, "counts": db.severity_counts(),
         "host_errors": db.host_error_counts(), "device_connected": relay.is_connected()}
    )


@app.post("/watchtower/overrides")
async def watchtower_overrides(request: Request) -> JSONResponse:
    """Manage severity-lowering rules (list / add / delete). Created from a log, managed in Settings."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    rules = _load_overrides()
    action = body.get("action")
    if action == "add":
        target = body.get("severity")
        if target not in SEVERITY_NAMES:
            return JSONResponse({"error": "bad severity"}, status_code=400)
        rules.append({"id": secrets.token_hex(4), "service": (body.get("service") or "").strip(),
                      "match": (body.get("match") or "").strip(), "severity": target})
        _save_overrides(rules)
    elif action == "delete":
        rules = [r for r in rules if r.get("id") != body.get("id")]
        _save_overrides(rules)
    return JSONResponse({"overrides": rules})


@app.post("/watchtower/print")
async def watchtower_print(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    entry = db.get_log(int(body.get("log_id", 0)))
    if not entry:
        return JSONResponse({"error": "Log not found"}, status_code=404)
    payload = _log_to_alert_payload(entry["device_id"], entry["severity"], entry["service"],
                                    entry["message"], entry["ts"])
    try:
        delivered = await _render_and_submit(payload, on_delivered=lambda: db.mark_printed(entry["id"]))
    except rendermod.RenderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    log.info("Manual print of log #%s (delivered=%s)", entry["id"], delivered)
    return JSONResponse({"ok": True, "delivered": delivered, "queued": not delivered})


@app.post("/watchtower/devices/create")
async def watchtower_devices_create(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        return JSONResponse({"error": "device_id required"}, status_code=400)
    secret = db.create_device(device_id, name=(body.get("name") or "").strip())
    log.info("Device created/rekeyed: %s", device_id)
    return JSONResponse({"ok": True, "device_id": device_id, "secret": secret})


@app.post("/watchtower/devices/rotate")
async def watchtower_devices_rotate(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    secret = db.rotate_device_secret((body.get("device_id") or "").strip())
    if secret is None:
        return JSONResponse({"error": "Device not found"}, status_code=404)
    return JSONResponse({"ok": True, "secret": secret})


@app.post("/watchtower/devices/revoke")
async def watchtower_devices_revoke(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"ok": db.revoke_device((body.get("device_id") or "").strip())})


@app.post("/watchtower/devices/update")
async def watchtower_devices_update(request: Request) -> JSONResponse:
    """Tell scout agent(s) to pull the latest scout.py from this server and restart. Body:
    {device_id} for one, or {all: true} for every non-revoked device. Delivered on their next
    poll (near-instant for connected agents)."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if body.get("all"):
        ids = [d["id"] for d in db.list_devices() if not d["revoked"]]
    else:
        did = (body.get("device_id") or "").strip()
        ids = [did] if did else []
    if not ids:
        return JSONResponse({"error": "No target devices"}, status_code=400)
    n = agents.queue_many(ids, {"cmd": "update"})
    log.info("Queued scout update for %d device(s): %s", n, ", ".join(ids))
    return JSONResponse({"ok": True, "queued": n})


_AGENT_CMDS = {"ping", "restart", "refresh-guests"}


@app.post("/watchtower/devices/command")
async def watchtower_devices_command(request: Request) -> JSONResponse:
    """Send a scout agent command: ``ping`` (agent replies with a visible pong log) or
    ``restart`` (agent re-execs, e.g. to pick up a new local scout.py). {device_id} or {all:true}.
    Delivered on the agent's next poll (near-instant while connected)."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    cmd = (body.get("cmd") or "").strip()
    if cmd not in _AGENT_CMDS:
        return JSONResponse({"error": f"cmd must be one of {sorted(_AGENT_CMDS)}"}, status_code=400)
    if body.get("all"):
        ids = [d["id"] for d in db.list_devices() if not d["revoked"]]
    else:
        did = (body.get("device_id") or "").strip()
        ids = [did] if did else []
    if not ids:
        return JSONResponse({"error": "No target devices"}, status_code=400)
    payload = {"cmd": cmd}
    if cmd == "ping":
        payload["ack"] = True  # manual ping -> agent logs a pong
    n = agents.queue_many(ids, payload)
    log.info("Queued scout '%s' for %d device(s): %s", cmd, n, ", ".join(ids))
    return JSONResponse({"ok": True, "queued": n})


@app.post("/watchtower/devices/run")
async def watchtower_devices_run(request: Request) -> JSONResponse:
    """Run a shell command on a scout agent; it ships stdout/stderr/exit back as a log
    (service ``scout.run``, no auto-print). Powerful, operator (session) only."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    device_id = (body.get("device_id") or "").strip()
    command = (body.get("command") or "").strip()
    if not device_id or not command:
        return JSONResponse({"error": "device_id and command required"}, status_code=400)
    agents.queue(device_id, {"cmd": "run", "command": command})
    log.info("Queued run on %s: %s", device_id, command[:120])
    return JSONResponse({"ok": True})


@app.post("/watchtower/devices/heartbeat")
async def watchtower_devices_heartbeat(request: Request) -> JSONResponse:
    """Set a device's expected reporting interval (seconds) for the dead-man's-switch. 0 disables."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    device_id = (body.get("device_id") or "").strip()
    try:
        seconds = int(body.get("seconds", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "seconds must be a number"}, status_code=400)
    if not db.set_heartbeat(device_id, seconds):
        return JSONResponse({"error": "Device not found"}, status_code=404)
    _silent_devices.discard(device_id)  # reset alert state
    return JSONResponse({"ok": True})


@app.post("/watchtower/metrics")
async def watchtower_metrics(request: Request) -> JSONResponse:
    """Time-bucketed error/other counts for the dashboard chart."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    hours = int(body.get("hours", 24))
    return JSONResponse(db.severity_timeseries(hours=hours, buckets=int(body.get("buckets", 48))))


@app.post("/watchtower/logs/export")
async def watchtower_logs_export(request: Request):
    """Export the current filtered logs as CSV or JSON."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    max_sev = body.get("max_sev")
    rows = db.list_logs(
        limit=int(body.get("limit", 5000)),
        max_sev=sev_num(max_sev) if max_sev else None,
        device_id=body.get("device_id") or None,
        service=body.get("service") or None,
        search=(body.get("search") or "").strip() or None,
    )
    if (body.get("format") or "csv") == "json":
        return JSONResponse(rows)
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "iso", "severity", "device_id", "service", "message", "printed"])
    import datetime
    for r in rows:
        w.writerow([r["ts"], datetime.datetime.fromtimestamp(r["ts"]).isoformat(timespec="seconds"),
                    r["severity"], r["device_id"], r["service"], r["message"], int(r["printed"])])
    return Response(content=buf.getvalue(), media_type="text/csv",
                   headers={"Content-Disposition": "attachment; filename=watchtower-logs.csv"})


@app.post("/watchtower/devices/delete")
async def watchtower_devices_delete(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    device_id = (body.get("device_id") or "").strip()
    if not db.delete_device(device_id, require_revoked=True):
        return JSONResponse({"error": "Device must be revoked before it can be deleted"}, status_code=400)
    log.info("Device deleted: %s", device_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Temp passwords + history (Passwords / History tabs)
# ---------------------------------------------------------------------------
@app.post("/admin/state")
async def admin_state(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"history": db.list_history(), "passwords": db.list_temp_passwords(),
                         "device_connected": relay.is_connected()})


@app.post("/admin/create")
async def admin_create(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        max_uses = int(body.get("max_uses", 1))
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_uses must be a number"}, status_code=400)
    if max_uses < 1:
        return JSONResponse({"error": "max_uses must be at least 1"}, status_code=400)
    pw = (body.get("new_password") or "").strip() or ("t_" + crypto.secrets.token_urlsafe(6))
    db.create_temp_password(pw, user=body.get("user", ""), max_uses=max_uses)
    return JSONResponse({"ok": True, "password": {"password": pw, "user": body.get("user", ""),
                                                  "max_uses": max_uses, "remaining": max_uses}})


@app.post("/admin/revoke")
async def admin_revoke(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"ok": db.revoke_temp_password((body.get("target") or "").strip())})


# ---------------------------------------------------------------------------
# WebSocket, POS app (HMAC only)
# ---------------------------------------------------------------------------
@app.websocket("/messages")
async def messages(ws: WebSocket) -> None:
    res = auth.verify_request("GET", "/messages", ws.headers, b"")
    if not res.ok:
        log.warning("Device WS rejected: %s", res.reason)
        await ws.close(code=4401)
        return
    auth_id = res.device_id or "default"
    await ws.accept()
    # The HMAC id (auth_id) authenticates the connection; on the relay the printer is the single
    # canonical print target "default", that's what is_connected() and submit() look for.
    client = await relay.register(ws, "default")
    log.info("Printer device connected: %s", auth_id)
    # Tag this device as the printer (distinct from scouts) even before its first status frame.
    pm = db.device_meta(auth_id)
    pm["role"] = "printer"
    db.touch_device(auth_id, meta=pm)

    # This socket carries print jobs (and only the Confer *mode switch*). The chat itself rides a
    # separate /confer/ws connection to whichever Confer server the printer is configured for, so
    # the Confer server and this print/internet-listener server can be different machines. A "mode:
    # confer" announcement here just pauses print jobs and flips this server's badge to "in Confer".
    try:
        while True:
            text = await ws.receive_text()
            try:
                frame = json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get("type") == "mode":
                mode = frame.get("mode")
                if mode == "confer":
                    await relay.set_confer_mode(client, True)
                    log.info("Printer %s announced Confer mode", auth_id)
                elif mode == "print":
                    await relay.set_confer_mode(client, False)
                    log.info("Printer %s returned to Print mode", auth_id)
            elif frame.get("type") == "printer_status":
                pm = db.device_meta(auth_id)
                pm["role"] = "printer"
                if "battery" in frame:
                    pm["battery"] = frame.get("battery")
                if "charging" in frame:
                    pm["charging"] = bool(frame.get("charging"))
                if "ready" in frame:
                    pm["ready"] = bool(frame.get("ready"))
                if "paper_out" in frame:
                    pm["paper_out"] = bool(frame.get("paper_out"))
                if "cover_open" in frame:
                    pm["cover_open"] = bool(frame.get("cover_open"))
                if frame.get("printer_state"):
                    pm["printer_state"] = str(frame.get("printer_state"))
                if frame.get("serial"):
                    pm["serial"] = str(frame.get("serial"))
                fires = _battery_updates(pm, auth_id)
                db.touch_device(auth_id, meta=pm)  # persist batt_alerted before firing
                for sev, svc, msg in fires:
                    await _fire_alert(auth_id, sev, svc, msg)
            # Any other frame is ignored (kept for forward-compat).
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await relay.unregister(client)
        log.info("Printer device disconnected: %s", auth_id)


# ---------------------------------------------------------------------------
# WebSocket, Confer live channel. Accepts EITHER a dashboard session token (the web admin) OR a
# Confer participant token (a printer chatting on this server). Both register with the hub and
# receive live fan-out; participants also get offline catch-up and may send read receipts.
# ---------------------------------------------------------------------------
@app.websocket("/confer/ws")
async def confer_ws(ws: WebSocket) -> None:
    token = ws.query_params.get("token")
    is_admin = auth.verify_session(token)
    user = None
    if not is_admin:
        uid = confer_sessions.resolve(token)
        user = db.confer_get_user(uid) if uid else None
        if not user:
            await ws.close(code=4401)
            return
    await ws.accept()

    async def _send_json(frame: dict) -> None:
        await ws.send_text(json.dumps(frame))

    conn = ConferConn(
        send=_send_json,
        user_id=None if is_admin else user["id"],
        username="admin" if is_admin else user["username"],
        display="Admin" if is_admin else user["display_name"],
        is_admin=is_admin,
    )
    await confer_hub.register(conn)
    log.info("Confer channel connected: %s", "admin" if is_admin else user["username"])
    if not is_admin:
        await confer_hub.deliver_catchup(conn)
    try:
        while True:
            text = await ws.receive_text()
            # Participants send read receipts to advance their offline-catch-up high-water mark.
            if conn.user_id is not None:
                try:
                    f = json.loads(text)
                    if isinstance(f, dict) and f.get("type") == "read":
                        db.confer_set_read(conn.user_id, int(f.get("chat_id") or 0),
                                           int(f.get("last_msg_id") or 0))
                except (ValueError, TypeError):
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await confer_hub.unregister(conn)
        log.info("Confer channel disconnected: %s", "admin" if is_admin else user["username"])


# ---------------------------------------------------------------------------
# Background retention pruning
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _startup() -> None:
    removed = db.prune_logs(retention_days(), err_retention_days())
    if removed:
        log.info("Pruned %d logs older than %d days", removed, retention_days())
    db.prune_sessions()
    db.confer_prune_messages(retention_days())
    # Know Proxmox scouts up front so a burst right after a restart still coalesces.
    _proxmox_devices.update(d["id"] for d in db.list_devices() if (d.get("meta") or {}).get("proxmox"))

    async def _pruner() -> None:
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                n = db.prune_logs(retention_days(), err_retention_days())
                if n:
                    log.info("Pruned %d expired logs", n)
                db.confer_prune_messages(retention_days())
            except Exception as exc:
                log.error("Log prune failed: %s", exc)

    async def _watchdog() -> None:
        # Dead-man's-switch: alert once when a device with a heartbeat interval goes silent,
        # and again (recovery) when it returns.
        while True:
            await asyncio.sleep(30)
            try:
                now = time.time()
                for d in db.list_devices():
                    hb = d.get("heartbeat_secs") or 0
                    if d["revoked"] or hb <= 0:
                        continue
                    last = d["last_seen_at"] or 0
                    silent = (now - last) > hb
                    was = d["id"] in _silent_devices
                    if silent and not was:
                        _silent_devices.add(d["id"])
                        ago = int(now - last) if last else -1
                        await _fire_alert(d["id"], "crit", "watchtower.silence",
                                          f"device SILENT, no report for {ago}s (expected every {hb}s)")
                    elif not silent and was:
                        _silent_devices.discard(d["id"])
                        await _fire_alert(d["id"], "notice", "watchtower.silence", "device recovered, reporting again")
            except Exception as exc:
                log.error("Watchdog failed: %s", exc)

    async def _pinger() -> None:
        # Periodically ping online agents so their presence + reported version stay fresh.
        # Silent (no "ack") so it doesn't flood the log stream; the poll it wakes re-reports version.
        while True:
            await asyncio.sleep(20)
            try:
                for d in db.list_devices():
                    if not d["revoked"] and agents.online(d["id"]):
                        agents.queue(d["id"], {"cmd": "ping"})
            except Exception as exc:
                log.error("Agent pinger failed: %s", exc)

    asyncio.create_task(_pruner())
    asyncio.create_task(_pinger())
    asyncio.create_task(_watchdog())
    try:
        await mqtt_bridge.start()
    except Exception as exc:
        log.error("MQTT bridge failed to start: %s", exc)
    try:
        await mqtt_client.start()
    except Exception as exc:
        log.error("MQTT client failed to start: %s", exc)
    log.info("Watchtower up. configured=%s auto-print<=%s fuse=%d/min retention=%dd",
             db.is_configured(), db.get_config("auto_print_min_sev", _DEF_MIN_SEV),
             auto_print_fuse(), retention_days())


@app.on_event("shutdown")
async def _shutdown() -> None:
    # On SIGTERM (systemctl restart, docker stop) close device sockets so they reconnect fast.
    await relay.close_all()
    await mqtt_bridge.stop()
    await mqtt_client.stop()
    log.info("Shutdown: closed device connections")
