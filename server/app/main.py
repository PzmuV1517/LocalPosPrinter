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

import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from . import render as rendermod
from .passwords import Store
from .relay import relay

# Master password. ACCESS_PASSWORD is the new name; ACCESS_CODE is still read for compat.
MASTER_PASSWORD = os.environ.get("ACCESS_PASSWORD") or os.environ.get("ACCESS_CODE", "1234")
PRINT_WIDTH = int(os.environ.get("PRINT_WIDTH", "384"))

_HERE = os.path.dirname(__file__)
_STATIC_DIR = os.path.join(os.path.dirname(_HERE), "static")

store = Store(MASTER_PASSWORD)
app = FastAPI(title="Sunmi Print Hub — companion server")


def _label_for(payload: dict) -> str:
    """A short human label for the history row."""
    return (payload.get("title") or payload.get("text") or payload.get("format") or "print")[:60]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "admin.html"))


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
    payload = await request.json()
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
        }
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
