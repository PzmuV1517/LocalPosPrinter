"""Standalone test for the Watchtower MQTT broker/bridge (starts a real broker on a high port)."""
import asyncio
import json
import logging
import tempfile

from amqtt.client import MQTTClient

from app import crypto
from app.db import Database
from app.mqtt_bridge import MqttBridge

logging.getLogger("amqtt").setLevel(logging.ERROR)
logging.getLogger("transitions").setLevel(logging.ERROR)


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="mqtt-test-")
    box = crypto.SecretBox(tmp)
    db = Database(tmp, box, lookup_key=box.derive("lk"))
    bridge = MqttBridge(db, tmp, logging.getLogger("test"))

    received: list = []

    async def on_message(kind, payload):
        received.append((kind, payload))
    bridge.on_message = on_message

    PORT = 18888
    bridge.save_settings({"enabled": True, "port": PORT, "username": "hauser",
                          "password": "s3cret", "prefix": "watchtower/"})
    await bridge.start()
    await asyncio.sleep(0.3)

    # A client with the wrong password must be rejected.
    bad = MQTTClient(config={"auto_reconnect": False})
    try:
        await bad.connect(f"mqtt://hauser:wrong@127.0.0.1:{PORT}/")
        rejected = False
        await bad.disconnect()
    except Exception:
        rejected = True
    assert rejected, "broker accepted a wrong password"
    print("  ok  MQTT rejects wrong password")

    # Authenticated publish reaches the bridge and is dispatched.
    pub = MQTTClient(config={"auto_reconnect": False})
    await pub.connect(f"mqtt://hauser:s3cret@127.0.0.1:{PORT}/")
    await pub.publish("watchtower/print", json.dumps({"format": "plain", "text": "via mqtt"}).encode(), qos=1)
    await pub.publish("watchtower/alert", json.dumps({"alert_type": "crit", "message": "disk"}).encode(), qos=1)
    await asyncio.sleep(0.6)
    await pub.disconnect()

    kinds = {k for k, _ in received}
    assert "print" in kinds and "alert" in kinds, f"missing dispatch: {received}"
    assert any(p.get("text") == "via mqtt" for k, p in received if k == "print")
    print("  ok  MQTT print + alert dispatched to the relay")

    await bridge.stop()
    print("\nMQTT BRIDGE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
