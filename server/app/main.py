"""
FastAPI companion server for Sunmi Print Hub.

Endpoints:
  GET  /                 web UI (compose + live preview)
  POST /preview          render a payload -> PNG (used by the web UI, debounced)
  POST /print            render a payload -> push to the connected device as image_raw_bitmap
  POST /check            check a password -> "no usage limit" / "X usages left" / invalid
  GET  /status           connection + queue status
  WS   /messages         the POS app connects here and receives pushed jobs
  GET  /admin            admin portal (gated by the master password)
  POST /admin/state      history + active passwords (master password required)
  POST /admin/create     create a limited-use password (master password required)
  POST /admin/revoke     revoke a password (master password required)

The web UI's Print reuses the identical render() call as Preview and ships its output as
image_raw_bitmap, so what you preview is exactly what prints.
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import render as rendermod
from .passwords import Store
from .relay import relay

# Master password. ACCESS_PASSWORD is the new name; ACCESS_CODE is still read for compat.
MASTER_PASSWORD = os.environ.get("ACCESS_PASSWORD") or os.environ.get("ACCESS_CODE", "1234")
PRINT_WIDTH = int(os.environ.get("PRINT_WIDTH", "384"))

# Fleet "Hershey Highway" broadcast channel. Every app's always-on fleet listener connects
# to /hersheyhighway with this shared static secret; the admin can broadcast to all of them.
# It's baked into the app, so treat it as "has the app => on the fleet", not strong auth.
# Overridable here so you can rotate it (must match the app's FleetConfig.CODE).
FLEET_CODE = os.environ.get("FLEET_CODE", "HersheyHighway42069")

_HERE = os.path.dirname(__file__)
_STATIC_DIR = os.path.join(os.path.dirname(_HERE), "static")

store = Store(MASTER_PASSWORD)
app = FastAPI(title="Sunmi Print Hub — companion server")

# Serve the alert font files so the web UI can preview each font in the picker.
app.mount("/fonts", StaticFiles(directory=os.path.join(_HERE, "fonts")), name="fonts")


def _label_for(payload: dict) -> str:
    """A short human label for the history row."""
    return (payload.get("title") or payload.get("text") or payload.get("format") or "print")[:60]


# The UI is edited often; tell the browser never to serve a cached copy so changes show up.
_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"), headers=_NO_CACHE)


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "admin.html"), headers=_NO_CACHE)


@app.get("/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "device_connected": relay.is_connected(),
            "pending_jobs": sum(len(q) for q in relay.pending.values()),
            "print_width": PRINT_WIDTH,
        }
    )


@app.post("/check")
async def check(request: Request) -> JSONResponse:
    """Non-consuming password check for the box next to the password field."""
    body = await request.json()
    info = store.check((body.get("password") or "").strip())
    if not info["valid"]:
        return JSONResponse({"valid": False, "message": "Invalid password"})
    if info["unlimited"]:
        return JSONResponse({"valid": True, "unlimited": True, "message": "No usage limit"})
    n = info["remaining"]
    return JSONResponse(
        {
            "valid": True,
            "unlimited": False,
            "remaining": n,
            "message": f"{n} usage{'s' if n != 1 else ''} left",
        }
    )


@app.post("/preview")
async def preview(request: Request) -> Response:
    payload = await request.json()
    try:
        img = rendermod.render(payload, PRINT_WIDTH)
    except rendermod.RenderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return Response(content=rendermod.to_png_bytes(img), media_type="image/png")


def _usage_message(unlimited: bool, remaining) -> str:
    if unlimited:
        return "no usage limit"
    return f"{remaining} usage{'s' if remaining != 1 else ''} left"


@app.post("/print")
async def do_print(request: Request) -> JSONResponse:
    return await _print_payload(await request.json())


@app.post("/alert")
async def do_alert(request: Request) -> JSONResponse:
    """MUIE alert intake for LAN services. Renders the envelope and relays it to the device.

    Body: { password, alert_type|type, message|text, service, sent_at, print_mode? }
    """
    body = await request.json()
    payload = {
        "password": body.get("password") or body.get("code"),
        "format": "alert",
        "alert_type": body.get("alert_type") or body.get("type") or "alert",
        "text": body.get("message") or body.get("text") or "",
        "service": body.get("service") or "",
        "sent_at": body.get("sent_at") or body.get("timestamp"),
        "print_mode": body.get("print_mode", "receipt"),
    }
    return await _print_payload(payload)


async def _print_payload(payload: dict) -> JSONResponse:
    # Accept 'password'; fall back to legacy 'code'.
    provided = (payload.get("password") or payload.get("code") or "").strip()

    # Validate WITHOUT consuming — a use is only deducted once the print actually goes through.
    info = store.check(provided)
    if not info["valid"]:
        return JSONResponse(
            {"error": "Invalid password or no usages left"}, status_code=401
        )

    # Render once, ship the exact pixels as image_raw_bitmap. A render failure costs no use.
    try:
        img = rendermod.render(payload, PRINT_WIDTH)
    except rendermod.RenderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    job = {
        # Send both keys so a device on either naming accepts it.
        "password": store.master,
        "code": store.master,
        "format": "image",
        "print_mode": payload.get("print_mode", "receipt"),
        "image_raw_bitmap": rendermod.to_base64_png(img),
    }

    fmt = payload.get("format", "?")
    label = _label_for(payload)

    # Deduct the use and log "printed" only when the relay actually hands the job to a device
    # (immediately, or later when a queued job flushes on reconnect).
    consumed: dict = {}

    def on_delivered() -> None:
        consumed.update(store.consume(provided))
        store.add_history(
            {"format": fmt, "label": label, "user": info["user"], "status": "printed"}
        )

    delivered = await relay.submit(job, on_delivered=on_delivered)

    if delivered:
        unlimited = consumed.get("unlimited", info["unlimited"])
        remaining = consumed.get("remaining", info["remaining"])
        return JSONResponse(
            {
                "ok": True,
                "delivered": True,
                "queued": False,
                "unlimited": unlimited,
                "remaining": remaining,
                "usage_message": _usage_message(unlimited, remaining),
                "message": "Sent to device",
            }
        )

    # Queued: nothing was consumed yet; record the queue and count the use only when it prints.
    store.add_history(
        {"format": fmt, "label": label, "user": info["user"], "status": "queued"}
    )
    return JSONResponse(
        {
            "ok": True,
            "delivered": False,
            "queued": True,
            "unlimited": info["unlimited"],
            "remaining": info["remaining"],
            "usage_message": _usage_message(info["unlimited"], info["remaining"]),
            "message": "No device connected — job queued (a use is counted only when it prints)",
        }
    )


# ---------------------------------------------------------------------------
# Admin portal (gated by the master password)
# ---------------------------------------------------------------------------
def _require_master(body: dict) -> bool:
    return store.is_master((body.get("password") or "").strip())


@app.post("/admin/state")
async def admin_state(request: Request) -> JSONResponse:
    body = await request.json()
    if not _require_master(body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(
        {
            "history": store.list_history(),
            "passwords": store.list_passwords(),
            "device_connected": relay.is_connected(),
            "fleet_count": relay.fleet_count(),
        }
    )


@app.post("/admin/broadcast")
async def admin_broadcast(request: Request) -> JSONResponse:
    """Broadcast one job to every printer on the fleet channel. Requires the master
    password (to be in admin) AND the app's static bypass code."""
    body = await request.json()
    if not _require_master(body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if (body.get("bypass_code") or "") != FLEET_CODE:
        return JSONResponse({"error": "Invalid bypass code"}, status_code=403)

    payload = dict(body.get("payload") or {})
    payload.pop("password", None)  # fleet devices trust the channel; no per-job password
    payload.pop("code", None)

    count = await relay.broadcast_fleet(payload)
    store.add_history(
        {
            "format": payload.get("format", "?"),
            "label": _label_for(payload),
            "user": "fleet-broadcast",
            "status": f"broadcast x{count}",
        }
    )
    return JSONResponse({"ok": True, "delivered": count, "fleet_count": relay.fleet_count()})


@app.post("/admin/fleet")
async def admin_fleet(request: Request) -> JSONResponse:
    """List devices connected to the fleet channel. Requires master password AND the
    correct bypass code (so it doubles as a 'check the bypass code' action)."""
    body = await request.json()
    if not _require_master(body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if (body.get("bypass_code") or "") != FLEET_CODE:
        return JSONResponse({"valid": False, "error": "Invalid bypass code"}, status_code=403)
    return JSONResponse(
        {"valid": True, "count": relay.fleet_count(), "devices": relay.list_fleet()}
    )


@app.post("/admin/create")
async def admin_create(request: Request) -> JSONResponse:
    body = await request.json()
    if not _require_master(body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        max_uses = int(body.get("max_uses", 1))
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_uses must be a number"}, status_code=400)
    if max_uses < 1:
        return JSONResponse({"error": "max_uses must be at least 1"}, status_code=400)
    tp = store.create(
        user=body.get("user", ""),
        max_uses=max_uses,
        password=body.get("new_password"),
    )
    return JSONResponse({"ok": True, "password": tp.to_public()})


@app.post("/admin/revoke")
async def admin_revoke(request: Request) -> JSONResponse:
    body = await request.json()
    if not _require_master(body):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ok = store.revoke((body.get("target") or "").strip())
    return JSONResponse({"ok": ok})


@app.websocket("/messages")
async def messages(ws: WebSocket) -> None:
    # Auth via ?password=... (legacy ?code=...) on the handshake. The app also sends an auth
    # frame on open, which we simply swallow below.
    provided = ws.query_params.get("password") or ws.query_params.get("code")
    if not store.is_master(provided):
        await ws.close(code=4401)  # policy violation / unauthorized
        return

    device_id = ws.query_params.get("device_id", "default")
    await ws.accept()
    client = await relay.register(ws, device_id)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await relay.unregister(client)


@app.websocket("/hersheyhighway")
async def hersheyhighway(ws: WebSocket) -> None:
    """Fleet broadcast channel. Every app connects here with the shared static bypass code;
    the admin can then broadcast one job to all of them at once, or check who's connected."""
    provided = ws.query_params.get("password") or ws.query_params.get("code")
    if provided != FLEET_CODE:
        await ws.close(code=4401)
        return
    # Real client IP: X-Forwarded-For's first hop (behind a proxy), else the socket peer.
    fwd = ws.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (ws.client.host if ws.client else "?")
    await ws.accept()
    client = await relay.register_fleet(ws, ip=ip)
    try:
        while True:
            msg = await ws.receive_text()
            # The app sends an auth/info frame on connect carrying device details.
            try:
                data = json.loads(msg)
                if isinstance(data, dict) and data.get("type") in ("auth", "info"):
                    client.info = {
                        k: data.get(k)
                        for k in ("device_id", "serial", "model", "version")
                        if data.get(k) is not None
                    }
            except (ValueError, TypeError):
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await relay.unregister_fleet(client)
