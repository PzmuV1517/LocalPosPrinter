#!/usr/bin/env python3
"""
Scout, the tiny log-shipper you drop on any device to report into Watchtower.

It signs every request with your device's HMAC secret (timestamp + nonce + body hash), so
nothing reusable is exposed even over a logged proxy. Errors (severity `err` and worse) are
auto-printed by the server; everything is browsable in the /watchtower dashboard.

No third-party dependencies, standard library only.

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
import threading
import time
import urllib.request

SEVERITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
SCOUT_VERSION = "2.3.2"

# journald PRIORITY (syslog) -> our severity names.
_JOURNAL_SEV = {0: "emerg", 1: "alert", 2: "crit", 3: "err", 4: "warning", 5: "notice", 6: "info", 7: "debug"}


def _collect_metrics() -> dict:
    """Best-effort host health (Linux, stdlib only): load, mem%, disk%, temp°C."""
    m = {}
    try:
        la = os.getloadavg()
        m["load1"] = round(la[0], 2)
        m["cpus"] = os.cpu_count() or 1
    except (OSError, AttributeError):
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = float(v.split()[0])
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        if total:
            m["mem_pct"] = round((1 - avail / total) * 100, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        s = os.statvfs("/")
        used = (s.f_blocks - s.f_bfree) * s.f_frsize
        total = s.f_blocks * s.f_frsize
        if total:
            m["disk_pct"] = round(used / total * 100, 1)
    except OSError:
        pass
    for zone in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(zone) as f:
                m["temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
                break
        except (OSError, ValueError):
            pass
    return m


def _detect_cameras() -> list:
    """UVC/webcam capture nodes (Linux, stdlib only). Each V4L2 device exposes several nodes; the
    one with index 0 is its capture interface, so the others (metadata/output) are skipped."""
    import glob
    cams = []
    for d in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            with open(os.path.join(d, "index")) as f:
                if f.read().strip() != "0":
                    continue
        except OSError:
            continue
        name = "camera"
        try:
            with open(os.path.join(d, "name")) as f:
                name = f.read().strip() or name
        except OSError:
            pass
        cams.append({"node": "/dev/" + os.path.basename(d), "name": name})
    return cams


_CAMS: dict = {}          # node -> capture thread, so a camera is only streamed once
_CAMS_LOCK = threading.Lock()


def _camera_stream(scout: "Scout", node: str, token: str, fps: int, size: str) -> None:
    """Capture one camera with ffmpeg (continuous MJPEG) and chunk-upload the raw bytes to the
    server, which splits frames and fans them out. The server closes the connection when the last
    viewer leaves; the failed write then tears ffmpeg down, so a camera runs only while watched.
    No forced resolution/framerate: many webcams reject an unsupported mode, so ffmpeg picks the
    camera's default. If it yields no video, ffmpeg's own error is reported to the dashboard."""
    import http.client
    import subprocess
    import urllib.parse
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "v4l2", "-i", node,
           "-vcodec", "mjpeg", "-q:v", "6", "-f", "mjpeg", "pipe:1"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        scout.log("warning", f"camera {node}: ffmpeg not installed (apt install ffmpeg)",
                  service="scout.camera")
        _CAMS.pop(node, None)
        return
    err_tail: list = []

    def _drain_err():
        for ln in proc.stderr:
            err_tail.append(ln)
            del err_tail[:-20]

    threading.Thread(target=_drain_err, daemon=True).start()
    u = urllib.parse.urlsplit(scout.url)
    Conn = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = Conn(u.netloc, timeout=20)
    path = "/agent/camera/push?token=" + urllib.parse.quote(token)
    sent = 0
    try:
        conn.putrequest("POST", path)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.endheaders()
        while True:
            data = proc.stdout.read(16384)
            if not data:
                break
            conn.send(b"%x\r\n" % len(data) + data + b"\r\n")
            sent += len(data)
    except Exception:
        pass  # server closed (no viewers) or network drop, tear down below
    finally:
        try:
            conn.send(b"0\r\n\r\n")
            conn.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        _CAMS.pop(node, None)
        if sent == 0:
            tail = b"".join(err_tail).decode(errors="replace").strip()[-400:]
            scout.log("warning", f"camera {node}: ffmpeg produced no video. {tail or '(no error output)'}",
                      service="scout.camera")
        else:
            # Confirms the scout->server leg worked; if the browser still saw nothing, the loss is
            # between the server and the browser (proxy buffering the feed).
            scout.log("info", f"camera {node}: streamed, sent {sent} bytes", service="scout.camera",
                      no_print=True)


def _start_camera(scout: "Scout", cmd: dict) -> None:
    node = cmd.get("node") or ""
    if not node:
        return
    with _CAMS_LOCK:
        if node in _CAMS:
            return
        t = threading.Thread(target=_camera_stream, args=(
            scout, node, cmd.get("token", ""), int(cmd.get("fps") or 10),
            str(cmd.get("size") or "640x480")), daemon=True)
        _CAMS[node] = t
        t.start()


def _describe_error(e: Exception) -> str:
    """A short, human reason why a poll failed, reported on reconnect."""
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

    def log(self, severity: str, message: str, service: str = "", meta: dict = None,
            no_print: bool = False) -> dict:
        if severity not in SEVERITIES:
            severity = "info"
        path = "/ingest"
        payload = {"severity": severity, "message": message, "service": service,
                   "meta": meta or {}, "ts": time.time()}
        if no_print:
            payload["no_print"] = True
        body = json.dumps(payload).encode()
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
        The poll itself is the heartbeat that keeps this device shown as online, and carries
        host health metrics for the dashboard."""
        path = "/agent/poll"
        body = json.dumps({"version": SCOUT_VERSION, "host": host, "metrics": _collect_metrics(),
                           "cameras": _detect_cameras()}).encode()
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


def _sevrank(name: str) -> int:
    try:
        return SEVERITIES.index(name)
    except ValueError:
        return SEVERITIES.index("info")


def _run_command(scout: "Scout", command: str) -> None:
    """Run a shell command from the dashboard; ship output back as a (no-print) log."""
    import subprocess
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        sev = "info" if r.returncode == 0 else "warning"
        scout.log(sev, f"$ {command}\n(exit {r.returncode})\n{out[:3000]}", service="scout.run",
                  meta={"command": command, "exit": r.returncode}, no_print=True)
    except Exception as e:
        scout.log("warning", f"$ {command}\n[error] {e}", service="scout.run",
                  meta={"command": command}, no_print=True)


def _forward_journald(scout: "Scout", floor: str, no_print: bool) -> None:
    import subprocess
    try:
        p = subprocess.Popen(["journalctl", "-f", "-n", "0", "-o", "json", "--no-pager"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        print("journalctl not found, journald forwarding disabled", file=sys.stderr)
        return
    for line in p.stdout:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        try:
            prio = int(e.get("PRIORITY", 6))
        except (TypeError, ValueError):
            prio = 6
        sev = _JOURNAL_SEV.get(prio, "info")
        if _sevrank(sev) > _sevrank(floor):
            continue
        msg = e.get("MESSAGE", "")
        if isinstance(msg, list):
            try:
                msg = bytes(msg).decode(errors="replace")
            except (ValueError, TypeError):
                msg = str(msg)
        unit = e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or "journald"
        scout.log(sev, str(msg)[:2000], service=str(unit), no_print=no_print)


def _forward_file(scout: "Scout", path: str, sev: str, floor: str, no_print: bool) -> None:
    import subprocess
    if _sevrank(sev) > _sevrank(floor):
        return
    try:
        p = subprocess.Popen(["tail", "-F", "-n", "0", path],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        return
    svc = os.path.basename(path)
    for line in p.stdout:
        line = line.rstrip("\n")
        if line:
            scout.log(sev, line[:2000], service=svc, no_print=no_print)


def _start_forwarders(scout: "Scout") -> list:
    """Start journald/file forwarders (in daemon threads) per SCOUT_FORWARD_* env vars."""
    import threading
    floor = os.environ.get("SCOUT_FORWARD_MIN_SEV", "warning")
    no_print = os.environ.get("SCOUT_FORWARD_NO_PRINT", "1") != "0"
    started = []
    if os.environ.get("SCOUT_FORWARD_JOURNALD", "0") == "1":
        threading.Thread(target=_forward_journald, args=(scout, floor, no_print), daemon=True).start()
        started.append("journald")
    for spec in [s.strip() for s in os.environ.get("SCOUT_FORWARD_FILES", "").split(",") if s.strip()]:
        path, _, sev = spec.rpartition(":") if ":" in spec else (spec, "", "info")
        threading.Thread(target=_forward_file, args=(scout, path.strip(), (sev or "info").strip(), floor, no_print),
                         daemon=True).start()
        started.append(path.strip())
    return started


def run_agent(scout: "Scout") -> int:
    """Persistent daemon: long-poll for commands (heartbeat + remote update). Reconnects fast,
    and reports its own presence to Watchtower (service ``scout.agent``): a log on start (covers a
    scout restart) and a log on reconnect that names why the connection was lost and how long it
    was down (covers a server restart)."""
    import socket
    host = socket.gethostname()
    print(f"scout agent {SCOUT_VERSION} -> {scout.url} as {scout.device_id} (host={host})")
    fwd = _start_forwarders(scout)
    if fwd:
        print(f"forwarding logs from: {', '.join(fwd)}")
    scout.log("info", f"scout agent {SCOUT_VERSION} started on {host}"
              + (f"; forwarding {', '.join(fwd)}" if fwd else ""), service="scout.agent",
              meta={"event": "start", "forwarding": fwd})
    connected = True
    lost_at = 0.0
    last_error = ""
    backoff = 1.0
    while True:
        try:
            cmd = scout.poll(host)
            if not connected:  # we just came back, ack it with the cause + downtime
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
                elif c == "run":
                    print("run command received")
                    _run_command(scout, cmd.get("command", ""))
                elif c == "camera" and cmd.get("action") == "start":
                    _start_camera(scout, cmd)
                elif c == "ping" and cmd.get("ack"):
                    # Manual ping, reply visibly. (Periodic pings omit "ack" and just refresh
                    # presence/version via this poll, so they don't flood the log stream.)
                    scout.log("info", "ping ack (pong)", service="scout.agent", meta={"event": "pong"})
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # server down / restart / network blip, re-poll shortly
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
