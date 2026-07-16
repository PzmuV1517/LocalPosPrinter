"""Watchtower-hosted MQTT broker and bridge.

Runs an embedded amqtt broker so external systems (Home Assistant, scripts) publish print/alert
jobs to the server rather than the phone. A subscriber relays them to the printer, same path as
manual or error prints.

Publish JSON to <prefix>print ({"format":"plain","text":"hi"}) or <prefix>alert
({"alert_type":"crit","service":"backup","message":"disk full"}). With a username/password set
in Settings (stored hashed) clients must authenticate, with none it allows anonymous connections
(LAN only). Broker failures never take down the main server.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from typing import Awaitable, Callable, Optional

from amqtt.broker import Broker
from amqtt.client import MQTTClient
from passlib.apps import custom_app_context

from .db import Database
from .mqtt_client import notify_discovery


class MqttBridge:
    def __init__(self, db: Database, data_dir: str, log):
        self.db = db
        self.data_dir = data_dir
        self.log = log
        # Set by main before start(): async (kind, payload_dict) -> None, kind in {"print","alert"}.
        self.on_message: Optional[Callable[[str, dict], Awaitable[None]]] = None
        self._broker: Optional[Broker] = None
        self._client: Optional[MQTTClient] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ---- config ----
    def get_settings(self) -> dict:
        g = self.db.get_config
        return {
            "enabled": g("mqtt_enabled", "0") == "1",
            "port": self.db.get_int("mqtt_port", 1883),
            "username": g("mqtt_user", "") or "",
            "has_password": bool(g("mqtt_pass_hash")),
            "prefix": g("mqtt_prefix", "watchtower/") or "watchtower/",
            "discovery": g("mqtt_discovery", "1") == "1",
        }

    def save_settings(self, body: dict) -> None:
        s = self.db.set_config
        if "enabled" in body:
            s("mqtt_enabled", "1" if body.get("enabled") else "0")
        if body.get("port") is not None:
            s("mqtt_port", int(body["port"]))
        if body.get("username") is not None:
            s("mqtt_user", str(body["username"]))
        if body.get("prefix"):
            p = str(body["prefix"])
            s("mqtt_prefix", p if p.endswith("/") else p + "/")
        if "discovery" in body:
            s("mqtt_discovery", "1" if body.get("discovery") else "0")
        if body.get("password"):
            s("mqtt_pass_hash", custom_app_context.hash(str(body["password"])))

    # ---- lifecycle ----
    async def start(self) -> None:
        st = self.get_settings()
        if not st["enabled"]:
            return
        port, prefix = st["port"], st["prefix"]
        user, user_hash = st["username"], self.db.get_config("mqtt_pass_hash")
        anonymous = not (user and user_hash)

        if anonymous:
            auth = {"allow-anonymous": True, "plugins": ["auth_anonymous"]}
            internal_uri = f"mqtt://127.0.0.1:{port}/"
        else:
            internal_user, internal_pw = "wt-internal", secrets.token_urlsafe(16)
            pwfile = os.path.join(self.data_dir, "mqtt_passwd")
            with open(pwfile, "w") as f:
                f.write(f"{user}:{user_hash}\n{internal_user}:{custom_app_context.hash(internal_pw)}\n")
            os.chmod(pwfile, 0o600)
            auth = {"allow-anonymous": False, "password-file": pwfile, "plugins": ["auth_file"]}
            internal_uri = f"mqtt://{internal_user}:{internal_pw}@127.0.0.1:{port}/"

        cfg = {
            "listeners": {"default": {"type": "tcp", "bind": f"0.0.0.0:{port}"}},
            "sys_interval": 0,
            "auth": auth,
            "topic-check": {"enabled": False},
        }
        self._broker = Broker(cfg)
        await self._broker.start()
        self._running = True
        self._task = asyncio.create_task(self._subscribe_loop(internal_uri, prefix))
        self.log.info("MQTT broker on :%d (anonymous=%s, prefix=%s)", port, anonymous, prefix)

    async def _subscribe_loop(self, uri: str, prefix: str) -> None:
        try:
            self._client = MQTTClient(config={"auto_reconnect": False})
            await self._client.connect(uri)
            await self._client.subscribe([(f"{prefix}print", 1), (f"{prefix}alert", 1)])
            # Publish HA availability + auto-discovery to the broker (retained), so an HA that
            # connects here sees a "Watchtower Printer" device without any manual YAML.
            await self._client.publish(f"{prefix}status", b"online", qos=1, retain=True)
            if self.db.get_config("mqtt_discovery", "1") == "1":
                topic, payload, _ = notify_discovery(prefix)
                await self._client.publish(topic, payload.encode(), qos=1, retain=True)
            while self._running:
                msg = await self._client.deliver_message()
                topic = msg.topic
                try:
                    payload = json.loads(msg.publish_packet.payload.data.decode())
                except (ValueError, AttributeError):
                    self.log.warning("MQTT: bad JSON payload on %s", topic)
                    continue
                kind = "alert" if topic.endswith("alert") else "print"
                if self.on_message and isinstance(payload, dict):
                    try:
                        await self.on_message(kind, payload)
                    except Exception as exc:
                        self.log.error("MQTT dispatch failed: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self._running:
                self.log.error("MQTT subscriber stopped: %s", exc)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        try:
            if self._client:
                await self._client.disconnect()
        except Exception:
            pass
        try:
            if self._broker:
                await self._broker.shutdown()
        except Exception:
            pass
        self._client = self._broker = None

    async def reload(self) -> None:
        await self.stop()
        try:
            await self.start()
        except Exception as exc:
            self.log.error("MQTT bridge failed to start: %s", exc)
