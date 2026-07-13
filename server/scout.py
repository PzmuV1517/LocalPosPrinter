#!/usr/bin/env python3
"""
Scout — the tiny log-shipper you drop on any device to report into Watchtower.

It signs every request with your device's HMAC secret (timestamp + nonce + body hash), so
nothing reusable is exposed even over a logged proxy. Errors (severity `err` and worse) are
auto-printed by the server; everything is browsable in the /watchtower dashboard.

No third-party dependencies — standard library only.

Set up a device in the Watchtower dashboard ("Issue device secret"), then:

    export WATCHTOWER_URL=https://watchtower.andreibanu.com
    export SCOUT_DEVICE_ID=kitchen-pi
    export SCOUT_SECRET=sph_xxxxxxxx        # shown once when you issued it

    # one-off from the shell
    python scout.py --severity err --service backup.service "snapshot failed: disk 98% full"

    # or from your own Python
    from scout import Scout
    scout = Scout()                          # reads the env vars above
    scout.log("err", "disk almost full", service="diskmon", meta={"pct": 98})
    scout.err("uncaught exception in worker", service="worker")
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.request

SEVERITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Scout:
    def __init__(self, url: str = None, device_id: str = None, secret: str = None, timeout: float = 5.0):
        self.url = (url or os.environ.get("WATCHTOWER_URL", "http://localhost:8000")).rstrip("/")
        self.device_id = device_id or os.environ.get("SCOUT_DEVICE_ID", "")
        self.secret = secret or os.environ.get("SCOUT_SECRET", "")
        self.timeout = timeout
        if not self.device_id or not self.secret:
            raise ValueError("Scout needs SCOUT_DEVICE_ID and SCOUT_SECRET (or constructor args)")

    def _sign(self, method: str, path: str, body: bytes) -> dict:
        ts = str(int(time.time()))
        nonce = secrets.token_urlsafe(12)
        signing = "\n".join([self.device_id, ts, nonce, method.upper(), path, _sha256_hex(body)])
        sig = hmac.new(self.secret.encode(), signing.encode(), hashlib.sha256).hexdigest()
        return {
            "X-Device-Id": self.device_id,
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sig,
            "Content-Type": "application/json",
        }

    def log(self, severity: str, message: str, service: str = "", meta: dict = None) -> dict:
        if severity not in SEVERITIES:
            severity = "info"
        path = "/ingest"
        body = json.dumps(
            {"severity": severity, "message": message, "service": service, "meta": meta or {}, "ts": time.time()}
        ).encode()
        req = urllib.request.Request(self.url + path, data=body, method="POST", headers=self._sign("POST", path, body))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"ok": False, "status": e.code, "error": e.read().decode(errors="replace")}
        except Exception as e:  # never let logging crash the caller
            return {"ok": False, "error": str(e)}

    # convenience shorthands
    def emerg(self, m, **k): return self.log("emerg", m, **k)
    def alert(self, m, **k): return self.log("alert", m, **k)
    def crit(self, m, **k): return self.log("crit", m, **k)
    def err(self, m, **k): return self.log("err", m, **k)
    def warning(self, m, **k): return self.log("warning", m, **k)
    def notice(self, m, **k): return self.log("notice", m, **k)
    def info(self, m, **k): return self.log("info", m, **k)
    def debug(self, m, **k): return self.log("debug", m, **k)


def _main() -> int:
    p = argparse.ArgumentParser(description="Ship a log event to Watchtower.")
    p.add_argument("message", help="the log message")
    p.add_argument("--severity", "-s", default="info", choices=SEVERITIES)
    p.add_argument("--service", default="", help="service/source name")
    p.add_argument("--meta", default="", help="optional JSON object of extra fields")
    p.add_argument("--url", default=None)
    p.add_argument("--device-id", default=None)
    p.add_argument("--secret", default=None)
    args = p.parse_args()
    meta = {}
    if args.meta:
        try:
            meta = json.loads(args.meta)
        except ValueError:
            print("--meta must be valid JSON", file=sys.stderr)
            return 2
    scout = Scout(url=args.url, device_id=args.device_id, secret=args.secret)
    result = scout.log(args.severity, args.message, service=args.service, meta=meta)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
