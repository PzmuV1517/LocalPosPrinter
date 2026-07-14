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
USERNAME = "admin"

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

resp = client.post("/setup", json={"username": USERNAME, "master_password": MASTER_PASSWORD, "print_width": 384,
                                    "auto_print_min_sev": "err", "auto_print_max_per_min": 0})
assert resp.status_code == 200 and resp.json().get("token")
print("  ok  POST /setup completes")
assert client.get("/setup/status").json()["configured"] is True
# Setup refuses to run twice.
assert client.post("/setup", json={"username": "x", "master_password": "yyyy"}).status_code == 409
print("  ok  /setup refuses re-run -> 409")

# ---- rendering endpoints (now auth-gated) ----
assert client.post("/preview", json={"format": "plain", "text": "hi"}).status_code == 401
resp = client.post("/preview", json={"format": "plain", "text": "hi", "password": MASTER_PASSWORD})
assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
print("  ok  POST /preview -> 401 unauth, png when authed", len(resp.content), "bytes")

resp = client.post("/preview", json={"format": "barcode", "text": "x", "password": MASTER_PASSWORD})
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
resp = client.post("/session/login", json={"username": USERNAME, "password": "nope"})
assert resp.status_code == 401
assert client.post("/session/login", json={"username": "wrong", "password": MASTER_PASSWORD}).status_code == 401
print("  ok  POST /session/login wrong password/username -> 401")

resp = client.post("/session/login", json={"username": USERNAME, "password": MASTER_PASSWORD})
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

# ---- agent heartbeat poll + remote update command (kitchen-pi still active) ----
assert client.post("/agent/poll", json={}).status_code == 401  # unsigned -> immediate 401
q = client.post("/watchtower/devices/update", json={"device_id": "kitchen-pi"}, headers=AUTH).json()
assert q["queued"] == 1
pbody = json.dumps({"version": "test", "host": "h"}).encode()
poll = client.post("/agent/poll", data=pbody, headers=sign("POST", "/agent/poll", pbody))
assert poll.status_code == 200 and poll.json()["cmd"] == {"cmd": "update"}
devs = client.post("/watchtower/logs", json={"limit": 1}, headers=AUTH).json()["devices"]
assert any(d["id"] == "kitchen-pi" and d.get("agent_online") for d in devs)
print("  ok  agent poll heartbeat + queued update delivered + shows online")

# ---- ping/restart commands via /watchtower/devices/command ----
assert client.post("/watchtower/devices/command", json={"device_id": "kitchen-pi", "cmd": "bogus"}, headers=AUTH).status_code == 400
assert client.post("/watchtower/devices/command", json={"device_id": "kitchen-pi", "cmd": "ping"}, headers=AUTH).json()["queued"] == 1
pbody = json.dumps({"version": "2.1.2", "host": "h"}).encode()
poll = client.post("/agent/poll", data=pbody, headers=sign("POST", "/agent/poll", pbody))
assert poll.json()["cmd"] == {"cmd": "ping", "ack": True}
# version reported on the poll shows up on the device
devs = client.post("/watchtower/logs", json={"limit": 1}, headers=AUTH).json()["devices"]
assert any(d["id"] == "kitchen-pi" and d["meta"].get("scout_version") == "2.1.2" for d in devs)
print("  ok  ping command delivered + scout version reported on poll")

# ---- heartbeat (dead-man's-switch), run command, metrics on poll ----
assert client.post("/watchtower/devices/heartbeat", json={"device_id": "kitchen-pi", "seconds": 60}, headers=AUTH).json()["ok"]
mbody = json.dumps({"version": "2.2.0", "host": "h", "metrics": {"disk_pct": 42.0, "mem_pct": 55}}).encode()
client.post("/agent/poll", data=mbody, headers=sign("POST", "/agent/poll", mbody))
devs = client.post("/watchtower/logs", json={"limit": 1}, headers=AUTH).json()["devices"]
kp = next(d for d in devs if d["id"] == "kitchen-pi")
assert kp["heartbeat_secs"] == 60 and kp["meta"]["metrics"]["disk_pct"] == 42.0
assert client.post("/watchtower/devices/run", json={"device_id": "kitchen-pi", "command": "uptime"}, headers=AUTH).json()["ok"]
rbody = json.dumps({"version": "2.2.0", "host": "h"}).encode()
assert client.post("/agent/poll", data=rbody, headers=sign("POST", "/agent/poll", rbody)).json()["cmd"] == {"cmd": "run", "command": "uptime"}
print("  ok  heartbeat set, metrics stored, run command delivered")

# ---- forwarded (no_print) err does not print ----
b = json.dumps({"severity": "err", "message": "nginx 500", "service": "nginx", "no_print": True}).encode()
r = client.post("/ingest", data=b, headers=sign("POST", "/ingest", b)).json()
assert r["printed"] is False and r["would_print"] is False
print("  ok  no_print err skips auto-print")

# ---- metrics timeseries + CSV export + notify config ----
ts = client.post("/watchtower/metrics", json={"hours": 24, "buckets": 24}, headers=AUTH).json()
assert len(ts["err"]) == 24 and len(ts["other"]) == 24
exp = client.post("/watchtower/logs/export", json={"format": "csv"}, headers=AUTH)
assert exp.status_code == 200 and exp.headers["content-type"].startswith("text/csv") and "severity" in exp.text
assert client.post("/config/set", json={"notify": {"enabled": True, "host": "smtp.x", "from_addr": "a@b",
       "to_addr": "c@d", "min_sev": "crit", "port": 587, "password": "pw"}}, headers=AUTH).json()["ok"]
n = client.post("/config/get", json={}, headers=AUTH).json()["notify"]
assert n["enabled"] and n["host"] == "smtp.x" and n["has_password"] is True
client.post("/config/set", json={"notify": {"enabled": False}}, headers=AUTH)  # off so no SMTP attempts later
print("  ok  metrics timeseries + CSV export + notify config roundtrip")

# ---- delete is refused for an active device, allowed once revoked ----
resp = client.post("/watchtower/devices/create", json={"device_id": "tmp-dev"}, headers=AUTH)
assert resp.status_code == 200
assert client.post("/watchtower/devices/delete", json={"device_id": "tmp-dev"}, headers=AUTH).status_code == 400
assert client.post("/watchtower/devices/revoke", json={"device_id": "tmp-dev"}, headers=AUTH).json()["ok"]
assert client.post("/watchtower/devices/delete", json={"device_id": "tmp-dev"}, headers=AUTH).json()["ok"]
devs = client.post("/watchtower/logs", json={"limit": 1}, headers=AUTH).json()["devices"]
assert not any(d["id"] == "tmp-dev" for d in devs)
print("  ok  device delete: 400 while active, 200 once revoked, then gone")

# ---- revoke the device; its signatures stop working ----
assert client.post("/watchtower/devices/revoke", json={"device_id": "kitchen-pi"}, headers=AUTH).json()["ok"]
body = json.dumps({"severity": "info", "message": "after revoke"}).encode()
assert client.post("/ingest", data=body, headers=sign("POST", "/ingest", body)).status_code == 401
print("  ok  revoked device -> 401")

# ---- Scout self-hosting: source + generated installer ----
r_scout = client.get("/scout.py")
assert r_scout.status_code == 200 and "class Scout" in r_scout.text
inst = client.get("/install-scout?device_id=kitchen-pi")
assert inst.status_code == 200 and "scout set-secret" in inst.text and "kitchen-pi" in inst.text
assert "__BASE__" not in inst.text and "__DEVICE__" not in inst.text  # placeholders substituted
print("  ok  /scout.py + /install-scout served")

# ---- temp password still works end to end ----
resp = client.post("/admin/create", json={"password": MASTER_PASSWORD, "user": "guest", "max_uses": 2}, headers=AUTH)
TEMP = resp.json()["password"]["password"]
assert client.post("/check", json={"target": TEMP}).status_code == 401  # now auth-gated
assert client.post("/check", json={"target": TEMP}, headers=AUTH).json()["remaining"] == 2
print("  ok  temp password created + /check (auth-gated) reports remaining")

# ---- security: healthz open, status/webauthn gated ----
assert client.get("/healthz").json()["ok"] is True
assert client.post("/status", json={}).status_code == 401
assert client.post("/status", json={}, headers=AUTH).json()["print_width"] == 384
assert client.post("/webauthn/list", json={}).status_code == 401
assert client.post("/webauthn/list", json={}, headers=AUTH).json()["passkeys"] == []
assert client.post("/webauthn/login/begin", json={}).status_code == 404  # no passkeys registered
sec = client.get("/healthz").headers
assert sec.get("content-security-policy") and sec.get("x-frame-options") == "DENY"
print("  ok  security: healthz open, status/webauthn/CSP gated")

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
assert client.post("/session/login", json={"username": USERNAME, "password": MASTER_PASSWORD}).status_code == 401
assert client.post("/session/login", json={"username": USERNAME, "password": "brand-new-pw"}).status_code == 200
print("  ok  master password change takes effect")

# ---- update/restart endpoints are auth-gated (don't trigger a real pull/restart here) ----
assert client.post("/config/update", json={}).status_code == 401
assert client.post("/config/restart", json={}).status_code == 401
print("  ok  /config/update and /config/restart require auth")

print("\nALL SMOKE TESTS PASSED")
