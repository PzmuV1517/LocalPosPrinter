"""Quick smoke test for the renderer + FastAPI endpoints. Not a real test suite."""
import base64
import io

from PIL import Image
from fastapi.testclient import TestClient

from app import render as r
from app.main import app, MASTER_PASSWORD

# A tiny PNG to feed the image path.
_buf = io.BytesIO()
Image.new("RGB", (200, 80), (128, 128, 128)).save(_buf, format="PNG")
IMG_B64 = base64.b64encode(_buf.getvalue()).decode()

cases = [
    {"format": "plain", "text": "Hello world\nsecond line that is quite long to force wrapping across"},
    {"format": "centered", "text": "Centered text"},
    {"format": "boxed", "text": "Boxed content\nwith two lines"},
    {"format": "boxed", "text": "ASCII @ border", "border_style": "at"},
    {"format": "boxed", "text": "Unicode box border here", "border_style": "double"},
    {"format": "boxed", "text": "bad style falls back", "border_style": "nonsense"},
    {"format": "plain", "text": 'top\n@#@divider="-="\nbottom'},
    {"format": "plain", "text": "cat:\n@#@cats"},
    {"format": "alert", "alert_type": "crit", "text": "Disk full on /var", "service": "diskmon.service", "sent_at": 1783732000},
    {"format": "alert", "alert_type": "warning", "text": "short", "service": "svc"},
    {"format": "header_body", "title": "RECEIPT", "text": "Body text here"},
    {"format": "banner", "title": "DOOR OPEN"},
    {"format": "list", "title": "Groceries", "items": [{"label": "Milk", "value": "x2"}, {"label": "Eggs", "value": "x12"}]},
    {"format": "qrcode", "text": "https://example.com"},
    {"format": "barcode", "text": "12345678", "barcode_type": "CODE128"},
    {"format": "image", "image": IMG_B64},
    {"format": "plain", "text": "text plus image", "image": IMG_B64, "image_position": "bottom"},
]

print(f"font regular={r._REGULAR_PATH} bold={r._BOLD_PATH}")
for c in cases:
    img = r.render(c, 384)
    assert img.width == 384, (c["format"], img.width)
    assert img.height > 0
    print(f"  ok  {c['format']:<12} -> {img.width}x{img.height}  mode={img.mode}")

# barcode missing type -> clear error
try:
    r.render({"format": "barcode", "text": "x"}, 384)
    raise SystemExit("expected RenderError for missing barcode_type")
except r.RenderError as e:
    print(f"  ok  barcode-missing-type raised: {e}")

# ---- endpoints ----
client = TestClient(app)

resp = client.post("/preview", json={"format": "plain", "text": "hi"})
assert resp.status_code == 200 and resp.headers["content-type"] == "image/png", resp.status_code
print("  ok  POST /preview -> png", len(resp.content), "bytes")

resp = client.post("/preview", json={"format": "barcode", "text": "x"})
assert resp.status_code == 400, resp.status_code
print("  ok  POST /preview bad -> 400")

resp = client.post("/print", json={"format": "plain", "text": "hi", "password": "wrong"})
assert resp.status_code == 401, resp.status_code
print("  ok  POST /print wrong code -> 401")

resp = client.post("/print", json={"format": "plain", "text": "hi", "password": MASTER_PASSWORD})
assert resp.status_code == 200, resp.status_code
data = resp.json()
assert data["queued"] is True  # no device connected in this test
print("  ok  POST /print queued (no device):", data["message"])

resp = client.get("/status")
assert resp.status_code == 200
print("  ok  GET /status:", resp.json())

print("ALL SMOKE TESTS PASSED")
