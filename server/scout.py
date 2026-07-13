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

Run it as an always-on agent for live presence in the dashboard + remote updates:

    scout agent            # long-polls the server; shows online; obeys "update" commands
    scout install-service  # install + enable a systemd --user unit that runs `scout agent`
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
SCOUT_VERSION = "2.1.2"


def _describe_error(e: Exception) -> str:
    """A short, human reason why a poll failed — reported on reconnect."""
    import http.client
    import socket as _socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return f"server returned HTTP {e.code}"
    reason = getattr(e, "reason", e)
    if isinstance(reason, (ConnectionResetError, http.client.RemoteDisconnected)):
        return "server dropped the connection (restart?)"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused (server down)"
    if isinstance(reason, (TimeoutError, _socket.timeout)) or isinstance(e, (TimeoutError, _socket.timeout)):
        return "network timeout"
    return str(reason) or e.__class__.__name__


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

    def poll(self, host: str, timeout: float = 35.0):
        """Long-poll the agent channel. Returns the next command dict, or None on timeout.
        The poll itself is the heartbeat that keeps this device shown as online."""
        path = "/agent/poll"
        body = json.dumps({"version": SCOUT_VERSION, "host": host}).encode()
        req = urllib.request.Request(self.url + path, data=body, method="POST",
                                     headers=self._sign("POST", path, body))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data.get("cmd")


def _self_update(scout: "Scout") -> None:
    """Download the latest scout.py from the server and re-exec the agent."""
    path = os.path.abspath(__file__)
    req = urllib.request.Request(scout.url + "/scout.py")
    data = urllib.request.urlopen(req, timeout=20).read()
    if b"class Scout" not in data:
        print("refusing update: downloaded file doesn't look like scout.py", file=sys.stderr)
        return
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    print(f"updated {path} -> restarting agent")
    os.execv(sys.executable, [sys.executable, path, "agent"])


def run_agent(scout: "Scout") -> int:
    """Persistent daemon: long-poll for commands (heartbeat + remote update). Reconnects fast,
    and reports its own presence to Watchtower (service ``scout.agent``): a log on start (covers a
    scout restart) and a log on reconnect that names why the connection was lost and how long it
    was down (covers a server restart)."""
    import socket
    host = socket.gethostname()
    print(f"scout agent {SCOUT_VERSION} -> {scout.url} as {scout.device_id} (host={host})")
    scout.log("info", f"scout agent {SCOUT_VERSION} started on {host}", service="scout.agent",
              meta={"event": "start"})
    connected = True
    lost_at = 0.0
    last_error = ""
    backoff = 1.0
    while True:
        try:
            cmd = scout.poll(host)
            if not connected:  # we just came back — ack it with the cause + downtime
                down = int(time.time() - lost_at)
                scout.log("notice", f"scout agent reconnected after {down}s down; cause: {last_error}",
                          service="scout.agent", meta={"event": "reconnect", "down_secs": down, "cause": last_error})
                print(f"reconnected after {down}s ({last_error})")
                connected = True
            backoff = 1.0
            if isinstance(cmd, dict):
                c = cmd.get("cmd")
                if c == "update":
                    print("update command received")
                    scout.log("info", "scout agent applying update from server", service="scout.agent",
                              meta={"event": "update"})
                    _self_update(scout)  # re-execs on success
                elif c == "restart":
                    print("restart command received")
                    scout.log("info", "scout agent restart requested from server", service="scout.agent",
                              meta={"event": "restart"})
                    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), "agent"])
                elif c == "ping" and cmd.get("ack"):
                    # Manual ping — reply visibly. (Periodic pings omit "ack" and just refresh
                    # presence/version via this poll, so they don't flood the log stream.)
                    scout.log("info", "ping ack (pong)", service="scout.agent", meta={"event": "pong"})
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # server down / restart / network blip — re-poll shortly
            if connected:
                connected = False
                lost_at = time.time()
                last_error = _describe_error(e)
                print(f"connection lost: {last_error}")
            time.sleep(min(backoff, 15))
            backoff = min(backoff * 2, 15)


def install_service() -> int:
    """Write + enable a systemd --user unit so the agent runs continuously."""
    import subprocess
    home = os.path.expanduser("~")
    launcher = os.path.join(home, ".local", "bin", "scout")
    exec_start = f"{launcher} agent" if os.path.exists(launcher) \
        else f"{sys.executable} {os.path.abspath(__file__)} agent"
    unit_dir = os.path.join(home, ".config", "systemd", "user")
    os.makedirs(unit_dir, exist_ok=True)
    unit_path = os.path.join(unit_dir, "scout-agent.service")
    with open(unit_path, "w") as f:
        f.write(
            "[Unit]\nDescription=Watchtower Scout agent\nAfter=network-online.target\n\n"
            f"[Service]\nExecStart={exec_start}\nRestart=always\nRestartSec=3\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
    print(f"wrote {unit_path}")
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "scout-agent"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"could not run: {' '.join(cmd)}\n  {r.stderr.strip()}", file=sys.stderr)
            print("Enable it manually: systemctl --user enable --now scout-agent")
            print("Keep it running after logout: loginctl enable-linger \"$USER\"")
            return 1
    print("scout-agent enabled and started.")
    print("Tip: run `loginctl enable-linger \"$USER\"` so it survives logout.")
    return 0


def _main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "agent":
        return run_agent(Scout())
    if argv and argv[0] == "install-service":
        return install_service()
    if argv and argv[0] in ("--version", "version"):
        print(SCOUT_VERSION)
        return 0

    p = argparse.ArgumentParser(description="Ship a log event to Watchtower (or run `scout agent`).")
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
