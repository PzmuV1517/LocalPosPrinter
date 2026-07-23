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
from app.main import app, _battery_updates, _apply_overrides  # noqa: E402
from app import main as _m  # noqa: E402
from scout import _parse_syslog  # noqa: E402

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

# ---- printer low-battery threshold crossings (fire once each, re-arm on recovery) ----
pm = {"battery": 22}
assert _battery_updates(pm, "printer1") == []                       # above all thresholds
pm["battery"] = 18
assert [f[0] for f in _battery_updates(pm, "printer1")] == ["warning"]  # crossed 20
assert _battery_updates(pm, "printer1") == []                       # still 18, no repeat
pm["battery"] = 9
assert [f[0] for f in _battery_updates(pm, "printer1")] == ["err"]   # crossed 10
pm["battery"] = 4
assert [f[0] for f in _battery_updates(pm, "printer1")] == ["crit"]  # crossed 5
pm["battery"] = 30                                                   # charged back up
assert _battery_updates(pm, "printer1") == [] and pm["batt_alerted"] == []  # all re-armed
pm["battery"] = 3                                                    # big drop past all three
assert [f[0] for f in _battery_updates(pm, "printer1")] == ["crit"]  # only most-severe fires
print("  ok  low-battery alerts fire once per threshold, re-arm on recovery")

# ---- scout syslog parsing (severity from PRI, host/tag best-effort) ----
assert _parse_syslog(b"<11>Oct 11 22:14:15 web01 nginx[123]: connect() failed") == ("err", "web01", "nginx", "connect() failed")
assert _parse_syslog(b"<13>1 2023-10-11T22:14:15Z host01 app 99 - - hello there") == ("notice", "host01", "app", "hello there")
assert _parse_syslog(b"no pri here")[0] == "info"
print("  ok  scout syslog parse maps PRI->severity and pulls host/tag")

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

# ---- cameras: reported on poll, operator selection persists, push rejects a bad token ----
# (meta is stored before the poll's command wait, so queue a ping first to return the poll fast.)
cbody = json.dumps({"version": "2.3.0", "host": "h",
                    "cameras": [{"node": "/dev/video0", "name": "USB Cam"}]}).encode()
def cam_poll():
    client.post("/watchtower/devices/command", json={"device_id": "kitchen-pi", "cmd": "ping"}, headers=AUTH)
    client.post("/agent/poll", data=cbody, headers=sign("POST", "/agent/poll", cbody))
def kitchen():
    return next(d for d in client.post("/watchtower/logs", json={"limit": 1}, headers=AUTH).json()["devices"] if d["id"] == "kitchen-pi")
cam_poll()
kp = kitchen()
assert kp["meta"]["cameras"] == [{"node": "/dev/video0", "name": "USB Cam"}]
assert kp["meta"]["metrics"]["disk_pct"] == 42.0  # earlier meta survived the merge, not clobbered
sel = client.post("/watchtower/camera/select", json={"device": "kitchen-pi", "node": "/dev/video0", "selected": True}, headers=AUTH)
assert sel.json()["cameras_selected"] == ["/dev/video0"]
cam_poll()  # selection must survive the next heartbeat (poll meta merge must not wipe it)
assert kitchen()["meta"]["cameras_selected"] == ["/dev/video0"]
# proxmox guest inventory rides the poll and lands in device meta
pbody = json.dumps({"version": "2.5.0", "host": "h",
                    "proxmox": {"guests": [{"vmid": 100, "name": "web", "kind": "ct", "status": "running"}]}}).encode()
client.post("/watchtower/devices/command", json={"device_id": "kitchen-pi", "cmd": "ping"}, headers=AUTH)
client.post("/agent/poll", data=pbody, headers=sign("POST", "/agent/poll", pbody))
assert kitchen()["meta"]["proxmox"]["guests"][0]["name"] == "web"
assert client.post("/agent/camera/push?token=bogus", content=b"x").status_code == 403
assert client.get("/watchtower/camera/stream?device=kitchen-pi&node=/dev/video0&token=bad").status_code == 401
print("  ok  cameras reported + selection persists across polls + push/stream auth gated")

# ---- severity overrides lower matching messages (never raise) + host_errors in logs ----
assert _apply_overrides("noisy.svc", "just a blip", "err") == "err"  # no rule yet
_ov = client.post("/watchtower/overrides", json={"action": "add", "service": "noisy.svc",
                  "match": "blip", "severity": "info"}, headers=AUTH).json()["overrides"]
assert len(_ov) == 1
assert _apply_overrides("noisy.svc", "just a blip", "err") == "info"      # lowered
assert _apply_overrides("noisy.svc", "real problem", "err") == "err"      # match miss -> unchanged
assert _apply_overrides("other.svc", "just a blip", "err") == "err"       # service miss -> unchanged
assert _apply_overrides("noisy.svc", "blip", "debug") == "debug"          # never raises
client.post("/watchtower/overrides", json={"action": "delete", "id": _ov[0]["id"]}, headers=AUTH)
assert client.post("/watchtower/overrides", json={}, headers=AUTH).json()["overrides"] == []
# hide rule: matching messages are dropped entirely at ingest
_hv = client.post("/watchtower/overrides", json={"action": "add", "match": "chatter",
                  "severity": "hide"}, headers=AUTH).json()["overrides"]
assert _apply_overrides("any.svc", "background chatter", "err") == "hide"
hbody = json.dumps({"severity": "err", "message": "background chatter here", "service": "x"}).encode()
resp = client.post("/ingest", data=hbody, headers=sign("POST", "/ingest", hbody)).json()
assert resp.get("hidden") is True and "id" not in resp  # dropped, not stored
client.post("/watchtower/overrides", json={"action": "delete", "id": _hv[0]["id"]}, headers=AUTH)
assert "host_errors" in client.post("/watchtower/logs", json={"limit": 1}, headers=AUTH).json()
assert client.post("/watchtower/devices/command", json={"device_id": "kitchen-pi", "cmd": "refresh-guests"}, headers=AUTH).json()["queued"] == 1
print("  ok  severity overrides lower logs + host_errors + refresh-guests command")

# ---- proxmox error-burst detection: tunable threshold, below no burst, flood trips it ----
client.post("/config/set", json={"burst_threshold": 20, "burst_window_secs": 10, "burst_summary_secs": 30}, headers=AUTH)
cfg = client.post("/config/get", json={}, headers=AUTH).json()
assert cfg["burst_threshold"] == 20 and cfg["burst_window_secs"] == 10 and cfg["burst_summary_secs"] == 30
_m._bursts.clear()
below = [_m._note_error_burst("pve1", "web01/nginx") for _ in range(19)]
assert not any(below)  # under the threshold, each handled individually
tripped = _m._note_error_burst("pve1", "db01/pg")
assert tripped and _m._bursts["pve1"].pending_total == 1  # now bursting, accumulating a summary
assert _m._note_error_burst("other", "x/y") is False  # a different (quiet) device is unaffected
client.post("/config/set", json={"burst_threshold": 0}, headers=AUTH)  # 0 disables
_m._bursts.clear()
assert not any(_m._note_error_burst("pve1", "x/y") for _ in range(30))
client.post("/config/set", json={"burst_threshold": 20}, headers=AUTH)  # restore
print("  ok  proxmox burst coalescer: tunable threshold, trips on flood, 0 disables")

# ---- printer WebSocket registers as the print target; /status shows online + print delivers ----
psec = client.post("/watchtower/devices/create", json={"device_id": "printer1", "name": "Printer"}, headers=AUTH).json()["secret"]
whdr = sign("GET", "/messages", b"", device_id="printer1", secret=psec)
assert client.post("/status", json={}, headers=AUTH).json()["device_connected"] is False
with client.websocket_connect("/messages", headers=whdr):
    assert client.post("/status", json={}, headers=AUTH).json()["device_connected"] is True
    assert client.post("/print", json={"format": "plain", "text": "hi"}, headers=AUTH).json()["delivered"] is True
print("  ok  printer WS (HMAC) -> device_connected True + print delivered")

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
# MQTT config roundtrip (broker left disabled so the smoke test doesn't bind a port)
assert client.post("/config/set", json={"mqtt": {"enabled": False, "port": 1883, "username": "ha",
       "password": "pw", "prefix": "watchtower"}}, headers=AUTH).json()["ok"]
mq = client.post("/config/get", json={}, headers=AUTH).json()["mqtt"]
assert mq["username"] == "ha" and mq["has_password"] is True and mq["prefix"] == "watchtower/"
print("  ok  metrics timeseries + CSV export + notify + mqtt config roundtrip")

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

# ---- public print page + temp-password preview + image adjustments ----
assert client.get("/public-print").status_code == 200
assert client.get("/public-print.js").status_code == 200
# preview works with a valid temp password (non-consuming), refused without
assert client.post("/preview", json={"format": "plain", "text": "hi"}).status_code == 401
assert client.post("/preview", json={"format": "plain", "text": "hi", "password": TEMP}).status_code == 200
_ib = io.BytesIO(); Image.new("RGB", (300, 200), (210, 200, 190)).save(_ib, "PNG")
_du = "data:image/png;base64," + base64.b64encode(_ib.getvalue()).decode()
# image adjustments are an operator feature (images aren't allowed on public/temp prints)
assert client.post("/preview", json={"format": "image", "image": _du,
       "image_contrast": 2.0, "image_dither": "threshold", "image_threshold": 150,
       "image_sharpen": True, "image_autocontrast": True}, headers=AUTH).status_code == 200
print("  ok  /public-print + temp-password preview + image adjustments (operator)")

# ---- public prints are size-capped and image-free (protect the paper roll) ----
assert client.post("/print", json={"format": "plain", "text": "x" * 700, "password": TEMP}).status_code == 400
assert client.post("/print", json={"format": "image", "image": _du, "password": TEMP}).status_code == 400
assert client.post("/preview", json={"format": "plain", "text": "x" * 700, "password": TEMP}).status_code == 400
# master/session is not capped
assert client.post("/print", json={"format": "plain", "text": "x" * 700}, headers=AUTH).status_code == 200
print("  ok  public print length cap + no images (master uncapped)")

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
