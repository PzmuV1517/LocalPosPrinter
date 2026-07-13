"""Quick smoke test for the renderer, auth, and FastAPI endpoints. Not a real test suite.

Runs against a throwaway DATA_DIR so it never touches real state. Covers the new security
surface: session login gate, HMAC-signed ingest, device issuance, and Watchtower queries.
"""
import base64
import hashlib
import hmac
import io
import json
import os
import tempfile
import time

# Isolate all state before importing the app. No ACCESS_PASSWORD -> exercises the web setup flow.
_TMP = tempfile.mkdtemp(prefix="printhub-smoke-")
os.environ["DATA_DIR"] = _TMP
os.environ.pop("ACCESS_PASSWORD", None)
os.environ.pop("ACCESS_CODE", None)
os.environ["AUTO_PRINT_MAX_PER_MIN"] = "0"  # no fuse during the test

from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import render as r  # noqa: E402
from app.main import app  # noqa: E402

MASTER_PASSWORD = "smoke-master-pw"

_buf = io.BytesIO()
Image.new("RGB", (200, 80), (128, 128, 128)).save(_buf, format="PNG")
IMG_B64 = base64.b64encode(_buf.getvalue()).decode()

cases = [
    {"format": "plain", "text": "Hello world\nsecond line that is quite long to force wrapping across"},
    {"format": "centered", "text": "Centered text"},
    {"format": "boxed", "text": "Boxed content\nwith two lines"},
    {"format": "alert", "alert_type": "crit", "text": "Disk full on /var", "service": "diskmon.service", "sent_at": 1783732000},
    {"format": "header_body", "title": "RECEIPT", "text": "Body text here"},
    {"format": "qrcode", "text": "https://example.com"},
    {"format": "image", "image": IMG_B64},
]

for c in cases:
    img = r.render(c, 384)
    assert img.width == 384 and img.height > 0, (c["format"], img.width)
    print(f"  ok  render {c['format']:<12} -> {img.width}x{img.height}")

client = TestClient(app)

# ---- first-run setup gate ----
assert client.get("/setup/status").json()["configured"] is False
print("  ok  /setup/status unconfigured")

# Before setup, protected data must be refused.
assert client.post("/watchtower/logs", json={}).status_code == 401
print("  ok  /watchtower/logs pre-setup -> 401")

resp = client.post("/setup", json={"master_password": MASTER_PASSWORD, "print_width": 384,
                                    "auto_print_min_sev": "err", "auto_print_max_per_min": 0})
assert resp.status_code == 200 and resp.json().get("token")
print("  ok  POST /setup completes")
assert client.get("/setup/status").json()["configured"] is True
# Setup refuses to run twice.
assert client.post("/setup", json={"master_password": "x"}).status_code == 409
print("  ok  /setup refuses re-run -> 409")

# ---- rendering endpoints ----
resp = client.post("/preview", json={"format": "plain", "text": "hi"})
assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
print("  ok  POST /preview -> png", len(resp.content), "bytes")

resp = client.post("/preview", json={"format": "barcode", "text": "x"})
assert resp.status_code == 400
print("  ok  POST /preview bad -> 400")

# ---- print auth ----
resp = client.post("/print", json={"format": "plain", "text": "hi", "password": "wrong"})
assert resp.status_code == 401
print("  ok  POST /print wrong password -> 401")

resp = client.post("/print", json={"format": "plain", "text": "hi", "password": MASTER_PASSWORD})
assert resp.status_code == 200 and resp.json()["queued"] is True
print("  ok  POST /print master queued (no device)")

# ---- session login gate ----
resp = client.post("/session/login", json={"password": "nope"})
assert resp.status_code == 401
print("  ok  POST /session/login wrong -> 401")

resp = client.post("/session/login", json={"password": MASTER_PASSWORD})
assert resp.status_code == 200
TOKEN = resp.json()["token"]
print("  ok  POST /session/login -> token")

assert client.post("/session/verify", headers={"Authorization": "Bearer " + TOKEN}).json()["ok"] is True
assert client.post("/session/verify", headers={"Authorization": "Bearer garbage"}).json()["ok"] is False
print("  ok  session verify accepts good token, rejects tampered")

# Watchtower data must be gated.
assert client.post("/watchtower/logs", json={}).status_code == 401
print("  ok  /watchtower/logs unauthenticated -> 401")

AUTH = {"Authorization": "Bearer " + TOKEN}

# ---- issue a device, then sign an ingest with it ----
resp = client.post("/watchtower/devices/create", json={"device_id": "kitchen-pi", "name": "Kitchen"}, headers=AUTH)
assert resp.status_code == 200
SECRET = resp.json()["secret"]
print("  ok  issued device secret", SECRET[:12] + "…")


def sign(method, path, body_bytes, device_id="kitchen-pi", secret=None, ts=None, nonce=None):
    secret = secret or SECRET
    ts = str(ts if ts is not None else int(time.time()))
    nonce = nonce or base64.urlsafe_b64encode(os.urandom(9)).decode()
    bh = hashlib.sha256(body_bytes).hexdigest()
    signing = "\n".join([device_id, ts, nonce, method.upper(), path, bh])
    sig = hmac.new(secret.encode(), signing.encode(), hashlib.sha256).hexdigest()
    return {"X-Device-Id": device_id, "X-Timestamp": ts, "X-Nonce": nonce,
            "X-Signature": sig, "Content-Type": "application/json"}


# Unsigned ingest is rejected.
assert client.post("/ingest", json={"severity": "info", "message": "hi"}).status_code == 401
print("  ok  /ingest unsigned -> 401")

# Bad signature rejected.
body = json.dumps({"severity": "info", "message": "hi"}).encode()
bad = sign("POST", "/ingest", body); bad["X-Signature"] = "deadbeef"
assert client.post("/ingest", data=body, headers=bad).status_code == 401
print("  ok  /ingest bad signature -> 401")

# Valid signed info log (info -> not printed).
body = json.dumps({"severity": "info", "message": "started up", "service": "boot"}).encode()
resp = client.post("/ingest", data=body, headers=sign("POST", "/ingest", body))
assert resp.status_code == 200 and resp.json()["printed"] is False and resp.json()["would_print"] is False
print("  ok  /ingest info accepted, not printed")

# Replay of the exact same signed request is rejected (nonce reuse).
hdrs = sign("POST", "/ingest", body)
assert client.post("/ingest", data=body, headers=hdrs).status_code == 200
assert client.post("/ingest", data=body, headers=hdrs).status_code == 401
print("  ok  /ingest nonce replay -> 401")

# Stale timestamp rejected.
old = sign("POST", "/ingest", body, ts=int(time.time()) - 10000)
assert client.post("/ingest", data=body, headers=old).status_code == 401
print("  ok  /ingest stale timestamp -> 401")

# err severity -> would_print True (queued since no device).
body = json.dumps({"severity": "err", "message": "worker crashed", "service": "worker"}).encode()
resp = client.post("/ingest", data=body, headers=sign("POST", "/ingest", body))
assert resp.status_code == 200 and resp.json()["would_print"] is True
print("  ok  /ingest err -> would_print True")

# ---- watchtower queries reflect the logs ----
resp = client.post("/watchtower/logs", json={}, headers=AUTH)
assert resp.status_code == 200
data = resp.json()
assert any(l["service"] == "worker" for l in data["logs"])
assert any(d["id"] == "kitchen-pi" for d in data["devices"])
print("  ok  /watchtower/logs returns logs + devices")

# Filter by severity.
resp = client.post("/watchtower/logs", json={"max_sev": "err"}, headers=AUTH)
assert all(l["sev_num"] <= 3 for l in resp.json()["logs"])
print("  ok  /watchtower/logs severity filter")

# ---- revoke the device; its signatures stop working ----
assert client.post("/watchtower/devices/revoke", json={"device_id": "kitchen-pi"}, headers=AUTH).json()["ok"]
body = json.dumps({"severity": "info", "message": "after revoke"}).encode()
assert client.post("/ingest", data=body, headers=sign("POST", "/ingest", body)).status_code == 401
print("  ok  revoked device -> 401")

# ---- temp password still works end to end ----
resp = client.post("/admin/create", json={"password": MASTER_PASSWORD, "user": "guest", "max_uses": 2}, headers=AUTH)
TEMP = resp.json()["password"]["password"]
assert client.post("/check", json={"password": TEMP}).json()["remaining"] == 2
print("  ok  temp password created + /check reports remaining")

# ---- session-authenticated print needs no password (operator is logged in) ----
resp = client.post("/print", json={"format": "plain", "text": "from operator"}, headers=AUTH)
assert resp.status_code == 200 and resp.json()["queued"] is True
print("  ok  POST /print via session token (no password) queued")

# ---- config get/set round-trips ----
assert client.post("/config/get", json={}).status_code == 401  # unauth
cfg = client.post("/config/get", json={}, headers=AUTH).json()
assert cfg["print_width"] == 384
assert client.post("/config/set", json={"print_width": 576}, headers=AUTH).json()["ok"]
assert client.post("/config/get", json={}, headers=AUTH).json()["print_width"] == 576
print("  ok  /config get/set round-trip")

# ---- changing the master password takes effect ----
assert client.post("/config/set", json={"new_master_password": "brand-new-pw"}, headers=AUTH).json()["ok"]
assert client.post("/session/login", json={"password": MASTER_PASSWORD}).status_code == 401
assert client.post("/session/login", json={"password": "brand-new-pw"}).status_code == 200
print("  ok  master password change takes effect")

print("\nALL SMOKE TESTS PASSED")
