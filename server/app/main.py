"""
FastAPI companion server for Sunmi Print Hub.

Endpoints:
  GET  /                 web UI (compose + live preview)
  POST /preview          render a payload -> PNG (used by the web UI, debounced)
  POST /print            render a payload -> push to the connected device as image_raw_bitmap
  GET  /status           connection + queue status
  WS   /messages         the POS app connects here and receives pushed jobs

The web UI's Print reuses the identical render() call as Preview and ships its output as
image_raw_bitmap, so what you preview is exactly what prints.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from . import render as rendermod
from .relay import relay

ACCESS_CODE = os.environ.get("ACCESS_CODE", "1234")
PRINT_WIDTH = int(os.environ.get("PRINT_WIDTH", "384"))

_HERE = os.path.dirname(__file__)
_STATIC_DIR = os.path.join(os.path.dirname(_HERE), "static")

app = FastAPI(title="Sunmi Print Hub — companion server")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "device_connected": relay.is_connected(),
            "pending_jobs": sum(len(q) for q in relay.pending.values()),
            "print_width": PRINT_WIDTH,
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


@app.post("/print")
async def do_print(request: Request) -> JSONResponse:
    payload = await request.json()

    if payload.get("code") != ACCESS_CODE:
        return JSONResponse({"error": "Invalid access code"}, status_code=401)

    # Render once, ship the exact pixels as image_raw_bitmap.
    try:
        img = rendermod.render(payload, PRINT_WIDTH)
    except rendermod.RenderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    job = {
        "code": ACCESS_CODE,
        "format": "image",
        "print_mode": payload.get("print_mode", "receipt"),
        "image_raw_bitmap": rendermod.to_base64_png(img),
    }
    delivered = await relay.submit(job)
    return JSONResponse(
        {
            "ok": True,
            "delivered": delivered,
            "queued": not delivered,
            "message": "Sent to device" if delivered else "No device connected — job queued",
        }
    )


@app.websocket("/messages")
async def messages(ws: WebSocket) -> None:
    # Auth via ?code=... on the handshake (the app also sends an auth frame, ignored here).
    code = ws.query_params.get("code")
    if code != ACCESS_CODE:
        await ws.close(code=4401)  # policy violation / unauthorized
        return

    device_id = ws.query_params.get("device_id", "default")
    await ws.accept()
    client = await relay.register(ws, device_id)
    try:
        while True:
            # We don't need anything the device sends; this just detects disconnects
            # (and swallows the optional auth frame the app sends on open).
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await relay.unregister(client)
