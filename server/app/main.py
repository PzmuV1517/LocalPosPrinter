"""
Watchtower — FastAPI companion server + fleet error/log dashboard for Sunmi Print Hub.

Two roles in one process:

  1. **Print relay** — the POS app keeps an outbound WebSocket here (``/messages``, HMAC-only);
     the Print tab and LAN services render jobs and we push them to the device.
  2. **Watchtower** — an observability platform. Small **Scout** clients sign log events to
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
import subprocess
import sys
import threading
import time
from collections import deque

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import crypto
from . import render as rendermod
from .agents import agents
from .auth import Auth
from .db import Database, sev_num
from .logging_setup import setup as setup_logging
from .relay import relay

# ---------------------------------------------------------------------------
# Config — env values are only *bootstrap defaults*; the source of truth is the DB config table,
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
auth = Auth(db, session_key=box.derive("session"), skew_secs=HMAC_SKEW_SECS)

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

app = FastAPI(title="Watchtower — Sunmi Print Hub")
app.mount("/fonts", StaticFiles(directory=os.path.join(_HERE, "fonts")), name="fonts")
_ASSETS_DIR = os.path.join(_WEB_DIST, "assets")
if os.path.isdir(_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")
else:  # dist not built/committed — the SPA won't load, but the API still runs
    log.warning("Web bundle missing at %s — run `npm --prefix web run build`.", _WEB_DIST)

_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


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


def _authed_admin(request: Request, body: dict) -> bool:
    """Operator auth for dashboard endpoints: valid session token OR the master password."""
    return _session_ok(request) or auth.is_master((body.get("password") or "").strip())


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


@app.get("/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "device_connected": relay.is_connected(),
            "pending_jobs": sum(len(q) for q in relay.pending.values()),
            "print_width": print_width(),
        }
    )


# ---------------------------------------------------------------------------
# Scout self-hosting — install the log-shipping client straight from this server, no git clone.
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
# Scout installer for Watchtower — downloads the client from your server and gets it ready
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

# Config is written once so re-running the installer keeps the secret you put in.
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
echo ""
echo "Finish setup — paste the secret shown in the Watchtower dashboard:"
[ -z "$DEVICE_ID" ] && echo "  $BIN/scout set-device <DEVICE_ID>"
echo "  $BIN/scout set-secret <SECRET>"
echo ""
echo "Test:  $BIN/scout -s info --service test \"hello watchtower\""
echo ""
echo "For live presence in the dashboard + remote updates, run the agent as a service"
echo "(after set-secret):"
echo "  $BIN/scout install-service"
if [ "$ONPATH" = "0" ]; then
  echo ""
  echo "('scout' will be on PATH in new shells. For THIS shell: export PATH=\"$BIN:\$PATH\")"
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
    _, body = await _read(request)
    token = auth.login((body.get("username") or "").strip(), (body.get("password") or "").strip())
    if not token:
        log.warning("Failed dashboard login from %s", _client_ip(request))
        return JSONResponse({"ok": False, "error": "Invalid username or password"}, status_code=401)
    log.info("Dashboard login from %s", _client_ip(request))
    return JSONResponse({"ok": True, "token": token})


@app.post("/session/verify")
async def session_verify(request: Request) -> JSONResponse:
    return JSONResponse({"ok": _session_ok(request)})


# ---------------------------------------------------------------------------
# Config (view/update; session-gated) — the web Settings tab
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


# ---------------------------------------------------------------------------
# Self-update — pull the latest main and restart, from the Settings tab.
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
                "log": "Not a git checkout — self-update is unavailable here."}
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


def _restart_process() -> None:
    cmd = os.environ.get("UPDATE_RESTART_CMD")
    try:
        log.info("Self-update: restarting (%s)", cmd or "os.execv in place")
        if cmd:
            subprocess.Popen(cmd, shell=True)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        log.error("Self-update restart failed: %s", exc)


@app.post("/config/restart")
async def config_restart(request: Request) -> JSONResponse:
    """Restart the service without pulling — same restart path as self-update, no git/pip."""
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    log.info("Manual restart requested from %s", _client_ip(request))
    await relay.close_all()
    threading.Timer(1.0, _restart_process).start()
    return JSONResponse({"ok": True, "restarting": True})


@app.post("/config/update")
async def config_update(request: Request) -> JSONResponse:
    _, body = await _read(request)
    if not _authed_admin(request, body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    result = await asyncio.to_thread(_run_update)
    log.info("Self-update requested: changed=%s %s->%s ok=%s",
             result.get("changed"), result.get("before"), result.get("after"), result.get("ok"))
    if result.get("restarting"):
        # Tell the printer(s) we're restarting so they reconnect the moment we're back up.
        await relay.close_all()
        # Fire after the response has been sent so the client sees the log.
        threading.Timer(1.5, _restart_process).start()
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Password check + rendering
# ---------------------------------------------------------------------------
@app.post("/check")
async def check(request: Request) -> JSONResponse:
    _, body = await _read(request)
    provided = (body.get("password") or "").strip()
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
    _, payload = await _read(request)
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
    """Render to exact pixels and push to the device (trusted HMAC channel — no per-job password)."""
    img = rendermod.render(payload, print_width())
    job = {
        "format": "image",
        "print_mode": payload.get("print_mode", "receipt"),
        "image_raw_bitmap": rendermod.to_base64_png(img),
    }
    return await relay.submit(job, on_delivered=on_delivered)


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

    try:
        img = rendermod.render(payload, print_width())
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
         "message": "No device connected — job queued (a temp use counts only when it prints)"}
    )


# ---------------------------------------------------------------------------
# Watchtower — log ingestion (HMAC only)
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
    if meta:
        db.touch_device(device_id, meta=meta)
    cmd = await agents.wait(device_id, timeout=25.0)
    return JSONResponse({"cmd": cmd})


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

    should_print = sev_num(severity) <= auto_print_max_num()
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
        log.warning("Auto-print fuse tripped (>%d/min) — %s/%s not printed", auto_print_fuse(), device_id, service)

    log_id = db.add_log(device_id=device_id, severity=severity, message=message, service=service,
                        meta=meta, source_ip=_client_ip(request), printed=printed)
    log.info("Ingest #%d [%s] %s/%s printed=%s", log_id, severity, device_id, service, printed)
    return JSONResponse({"ok": True, "id": log_id, "printed": printed, "would_print": should_print})


# ---------------------------------------------------------------------------
# Watchtower — dashboard data (session or master password)
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
         "device_connected": relay.is_connected()}
    )


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
# WebSocket — POS app (HMAC only)
# ---------------------------------------------------------------------------
@app.websocket("/messages")
async def messages(ws: WebSocket) -> None:
    res = auth.verify_request("GET", "/messages", ws.headers, b"")
    if not res.ok:
        log.warning("Device WS rejected: %s", res.reason)
        await ws.close(code=4401)
        return
    device_id = res.device_id or "default"
    await ws.accept()
    client = await relay.register(ws, device_id)
    log.info("Printer device connected: %s", device_id)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await relay.unregister(client)
        log.info("Printer device disconnected: %s", device_id)


# ---------------------------------------------------------------------------
# Background retention pruning
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _startup() -> None:
    removed = db.prune_logs(retention_days())
    if removed:
        log.info("Pruned %d logs older than %d days", removed, retention_days())

    async def _pruner() -> None:
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                n = db.prune_logs(retention_days())
                if n:
                    log.info("Pruned %d expired logs", n)
            except Exception as exc:
                log.error("Log prune failed: %s", exc)

    asyncio.create_task(_pruner())
    log.info("Watchtower up. configured=%s auto-print<=%s fuse=%d/min retention=%dd",
             db.is_configured(), db.get_config("auto_print_min_sev", _DEF_MIN_SEV),
             auto_print_fuse(), retention_days())


@app.on_event("shutdown")
async def _shutdown() -> None:
    # On SIGTERM (systemctl restart, docker stop) close device sockets so they reconnect fast.
    await relay.close_all()
    log.info("Shutdown: closed device connections")
