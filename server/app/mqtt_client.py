"""
Watchtower as an MQTT **client** (as opposed to :mod:`mqtt_bridge`, which runs a broker here).

This connects OUT to an existing broker you already run — e.g. Home Assistant's Mosquitto — and:

  * publishes **Home Assistant discovery** so a "Watchtower Printer" device appears automatically
    (a ``notify`` entity whose command relays to the printer),
  * subscribes to ``<prefix>print`` / ``<prefix>alert`` and relays those to the printer over the
    existing WebSocket (same ``on_message`` path as the built-in broker),
  * publishes a retained ``<prefix>status`` availability topic (with an ``offline`` LWT).

Runs *alongside* the built-in broker — pick either or both in Settings. The broker password we
need to authenticate to your broker is stored **encrypted** (SecretBox), since we must send it.
Every failure is caught and retried so a broker being down never affects the main server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

from amqtt.client import MQTTClient

from .crypto import SecretBox
from .db import Database

NODE_ID = "watchtower_printer"


def notify_discovery(prefix: str) -> tuple[str, str, str]:
    """Return (config_topic, payload_json, status_topic) for the Home Assistant notify device.

    Shared by both MQTT modes (hosted broker and outbound client) so they publish the identical
    device — HA dedupes on the unique_id, so running both never makes two printers.
    """
    print_topic, status_topic = f"{prefix}print", f"{prefix}status"
    device = {
        "identifiers": [NODE_ID], "name": "Watchtower Printer",
        "manufacturer": "Watchtower", "model": "MQTT bridge",
    }
    # HA's notify title/message map onto our title/text. No password needed — the broker
    # connection is already authenticated and Watchtower relays as a trusted source.
    command_template = (
        "{\"format\":\"{{ data.format | default('plain') }}\","
        "\"print_mode\":\"{{ data.print_mode | default('receipt') }}\","
        "\"title\":{{ (title if title is defined and title else '') | to_json }},"
        "\"text\":{{ (message if message is defined else '') | to_json }}"
        "{% if data.barcode_type is defined %},\"barcode_type\":{{ data.barcode_type | to_json }}{% endif %}"
        "{% if data.items is defined %},\"items\":{{ data.items | to_json }}{% endif %}"
        "{% if data.image is defined %},\"image\":{{ data.image | to_json }}{% endif %}"
        "{% if data.font is defined %},\"font\":{{ data.font | to_json }}{% endif %}"
        "}"
    )
    cfg = {
        "name": "Watchtower Printer", "unique_id": f"{NODE_ID}_notify",
        "command_topic": print_topic, "command_template": command_template,
        "availability_topic": status_topic,
        "payload_available": "online", "payload_not_available": "offline",
        "device": device,
    }
    return f"homeassistant/notify/{NODE_ID}/config", json.dumps(cfg), status_topic


class MqttClientBridge:
    def __init__(self, db: Database, box: SecretBox, log):
        self.db = db
        self.box = box
        self.log = log
        # Set by main before start(): async (kind, payload_dict) -> None, kind in {"print","alert"}.
        self.on_message: Optional[Callable[[str, dict], Awaitable[None]]] = None
        self._client: Optional[MQTTClient] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._connected = False
        self._last_error = ""

    # ---- config ----
    def get_settings(self) -> dict:
        g = self.db.get_config
        return {
            "enabled": g("mqtt_client_enabled", "0") == "1",
            "host": g("mqtt_client_host", "") or "",
            "port": self.db.get_int("mqtt_client_port", 1883),
            "username": g("mqtt_client_user", "") or "",
            "has_password": bool(g("mqtt_client_pass_enc")),
            "tls": g("mqtt_client_tls", "0") == "1",
            "prefix": g("mqtt_client_prefix", "watchtower/") or "watchtower/",
            "discovery": g("mqtt_client_discovery", "1") == "1",
            "connected": self._connected,
            "last_error": self._last_error,
        }

    def save_settings(self, body: dict) -> None:
        s = self.db.set_config
        if "enabled" in body:
            s("mqtt_client_enabled", "1" if body.get("enabled") else "0")
        if body.get("host") is not None:
            s("mqtt_client_host", str(body["host"]).strip())
        if body.get("port") is not None:
            s("mqtt_client_port", int(body["port"]))
        if body.get("username") is not None:
            s("mqtt_client_user", str(body["username"]))
        if "tls" in body:
            s("mqtt_client_tls", "1" if body.get("tls") else "0")
        if "discovery" in body:
            s("mqtt_client_discovery", "1" if body.get("discovery") else "0")
        if body.get("prefix"):
            p = str(body["prefix"])
            s("mqtt_client_prefix", p if p.endswith("/") else p + "/")
        if body.get("password"):
            s("mqtt_client_pass_enc", self.box.encrypt(str(body["password"])))

    def _prefix(self) -> str:
        p = self.db.get_config("mqtt_client_prefix", "watchtower/") or "watchtower/"
        return p if p.endswith("/") else p + "/"

    def _uri(self, st: dict) -> str:
        scheme = "mqtts" if st["tls"] else "mqtt"
        auth = ""
        if st["username"]:
            auth = quote(st["username"], safe="")
            enc = self.db.get_config("mqtt_client_pass_enc", "")
            pw = self.box.decrypt(enc) if enc else ""
            if pw:
                auth += ":" + quote(pw, safe="")
            auth += "@"
        return f"{scheme}://{auth}{st['host']}:{st['port']}/"

    # ---- lifecycle ----
    async def start(self) -> None:
        st = self.get_settings()
        if not st["enabled"] or not st["host"]:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(st))
        self.log.info("MQTT client → %s:%d (prefix=%s, discovery=%s)",
                      st["host"], st["port"], st["prefix"], st["discovery"])

    async def _run(self, st: dict) -> None:
        prefix = st["prefix"]
        status_topic = f"{prefix}status"
        # amqtt validates will.message as a str (not bytes).
        will = {"topic": status_topic, "message": "offline", "qos": 1, "retain": True}
        while self._running:
            try:
                self._client = MQTTClient(config={"auto_reconnect": False, "will": will})
                await self._client.connect(self._uri(st))
                await self._client.publish(status_topic, b"online", qos=1, retain=True)
                if st["discovery"]:
                    await self._publish_discovery(prefix)
                await self._client.subscribe([(f"{prefix}print", 1), (f"{prefix}alert", 1)])
                self._connected = True
                self._last_error = ""
                self.log.info("MQTT client connected to %s:%d", st["host"], st["port"])
                while self._running:
                    msg = await self._client.deliver_message()
                    topic = msg.topic
                    try:
                        payload = json.loads(msg.publish_packet.payload.data.decode())
                    except (ValueError, AttributeError):
                        self.log.warning("MQTT client: bad JSON payload on %s", topic)
                        continue
                    kind = "alert" if topic.endswith("alert") else "print"
                    if self.on_message and isinstance(payload, dict):
                        try:
                            await self.on_message(kind, payload)
                        except Exception as exc:
                            self.log.error("MQTT client dispatch failed: %s", exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc) or type(exc).__name__
                if self._running:
                    self.log.warning("MQTT client link down (%s); retrying in 5s", exc)
                    try:
                        if self._client:
                            await self._client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(5)
        self._connected = False

    async def _publish_discovery(self, prefix: str) -> None:
        topic, payload, _ = notify_discovery(prefix)
        await self._client.publish(topic, payload.encode(), qos=1, retain=True)

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        try:
            if self._client:
                await self._client.publish(f"{self._prefix()}status", b"offline", qos=1, retain=True)
                await self._client.disconnect()
        except Exception:
            pass
        self._client = None

    async def reload(self) -> None:
        await self.stop()
        try:
            await self.start()
        except Exception as exc:
            self.log.error("MQTT client failed to start: %s", exc)
