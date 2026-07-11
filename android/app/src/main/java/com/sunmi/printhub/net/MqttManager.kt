package com.sunmi.printhub.net

import android.content.Context
import android.util.Log
import com.sunmi.printhub.BuildConfig
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.core.PrintDispatcher
import com.sunmi.printhub.db.JobSource
import org.eclipse.paho.android.service.MqttAndroidClient
import org.eclipse.paho.client.mqttv3.IMqttActionListener
import org.eclipse.paho.client.mqttv3.IMqttDeliveryToken
import org.eclipse.paho.client.mqttv3.IMqttToken
import org.eclipse.paho.client.mqttv3.MqttCallbackExtended
import org.eclipse.paho.client.mqttv3.MqttConnectOptions
import org.eclipse.paho.client.mqttv3.MqttMessage
import org.json.JSONObject

/**
 * MQTT client with built-in Home Assistant discovery. Publishes a retained availability
 * topic (with an "offline" LWT), subscribes for print jobs, and publishes a lastjob summary.
 *
 * Reconnect uses Paho's automatic reconnect (its own exponential backoff, capped) so we
 * don't hammer the broker.
 */
class MqttManager(private val context: Context) {

    companion object {
        private const val TAG = "MqttManager"
        private const val NODE_ID = "sunmi_printhub"
    }

    private val settings = Hub.settings
    private var client: MqttAndroidClient? = null

    private val prefix get() = settings.mqttPrefix
    private val printTopic get() = prefix + "print"
    private val statusTopic get() = prefix + "status"
    private val lastJobTopic get() = prefix + "lastjob"

    fun start() {
        if (settings.mqttHost.isBlank()) {
            Log.w(TAG, "MQTT host not configured; skipping")
            return
        }
        val scheme = if (settings.mqttTls) "ssl" else "tcp"
        val uri = "$scheme://${settings.mqttHost}:${settings.mqttPort}"
        val clientId = NODE_ID + "_" + System.currentTimeMillis()
        val c = MqttAndroidClient(context, uri, clientId)
        client = c

        c.setCallback(object : MqttCallbackExtended {
            override fun connectComplete(reconnect: Boolean, serverURI: String?) {
                Log.i(TAG, "MQTT connected (reconnect=$reconnect)")
                onConnected()
            }

            override fun connectionLost(cause: Throwable?) {
                Log.w(TAG, "MQTT connection lost", cause)
            }

            override fun messageArrived(topic: String?, message: MqttMessage?) {
                if (topic == printTopic && message != null) {
                    val body = String(message.payload)
                    PrintDispatcher.dispatchJson(body, JobSource.MQTT, sourceInfo = "mqtt")
                }
            }

            override fun deliveryComplete(token: IMqttDeliveryToken?) {}
        })

        val opts = MqttConnectOptions().apply {
            isAutomaticReconnect = true
            isCleanSession = true
            connectionTimeout = 10
            keepAliveInterval = 30
            if (settings.mqttUser.isNotBlank()) userName = settings.mqttUser
            if (settings.mqttPass.isNotBlank()) password = settings.mqttPass.toCharArray()
            setWill(statusTopic, "offline".toByteArray(), 1, true)
        }

        try {
            c.connect(opts, null, object : IMqttActionListener {
                override fun onSuccess(asyncActionToken: IMqttToken?) {}
                override fun onFailure(asyncActionToken: IMqttToken?, exception: Throwable?) {
                    Log.e(TAG, "MQTT connect failed", exception)
                }
            })
        } catch (t: Throwable) {
            Log.e(TAG, "MQTT connect threw", t)
        }

        // Publish lastjob/status for jobs from every source.
        Hub.jobCompleteListener = { result, source -> publishLastJob(result, source) }
    }

    private fun onConnected() {
        publish(statusTopic, "online", retained = true)
        try {
            client?.subscribe(printTopic, 1)
        } catch (t: Throwable) {
            Log.e(TAG, "subscribe failed", t)
        }
        publishHaDiscovery()
    }

    fun stop() {
        Hub.jobCompleteListener = null
        try {
            publish(statusTopic, "offline", retained = true)
            client?.disconnect()
        } catch (_: Throwable) {
        }
        try {
            client?.close()
        } catch (_: Throwable) {
        }
        client = null
    }

    fun isConnected(): Boolean = try {
        client?.isConnected == true
    } catch (_: Throwable) {
        false
    }

    private fun publish(topic: String, payload: String, retained: Boolean = false, qos: Int = 1) {
        val c = client ?: return
        try {
            c.publish(topic, payload.toByteArray(), qos, retained)
        } catch (t: Throwable) {
            Log.e(TAG, "publish to $topic failed", t)
        }
    }

    private fun publishLastJob(result: PrintDispatcher.Result, source: JobSource) {
        val obj = JSONObject()
            .put("source", source.wire)
            .put("format", result.format)
            .put("status", result.status.wire)
            .put("timestamp", System.currentTimeMillis())
        result.error?.let { obj.put("error", it) }
        publish(lastJobTopic, obj.toString(), retained = true)
    }

    // ---- Home Assistant discovery ----

    private fun deviceBlock(): JSONObject = JSONObject()
        .put("identifiers", org.json.JSONArray(listOf(NODE_ID)))
        .put("name", "Sunmi Print Hub")
        .put("manufacturer", "SUNMI")
        .put("model", "V2 Pro Print Hub")
        .put("sw_version", BuildConfig.VERSION_NAME)

    private fun availabilityBlock(cfg: JSONObject) {
        cfg.put("availability_topic", statusTopic)
            .put("payload_available", "online")
            .put("payload_not_available", "offline")
    }

    private fun publishHaDiscovery() {
        // Notify entity — maps HA's title+message straight onto our title/text fields, with
        // the access password baked into the command_template so notify.sunmi_printhub just works.
        val pw = settings.accessPassword
        val commandTemplate = buildString {
            append("{\"password\":\"").append(pw).append("\",")
            append("\"format\":\"{{ data.format | default('plain') }}\",")
            append("\"print_mode\":\"{{ data.print_mode | default('receipt') }}\",")
            append("\"title\":{{ (title if title is defined and title else '') | to_json }},")
            append("\"text\":{{ (message if message is defined else '') | to_json }}")
            append("{% if data.barcode_type is defined %},\"barcode_type\":{{ data.barcode_type | to_json }}{% endif %}")
            append("{% if data.items is defined %},\"items\":{{ data.items | to_json }}{% endif %}")
            append("{% if data.image is defined %},\"image\":{{ data.image | to_json }}{% endif %}")
            append("{% if data.image_raw_bitmap is defined %},\"image_raw_bitmap\":{{ data.image_raw_bitmap | to_json }}{% endif %}")
            append("{% if data.image_position is defined %},\"image_position\":{{ data.image_position | to_json }}{% endif %}")
            append("}")
        }
        val notifyCfg = JSONObject()
            .put("name", "Sunmi PrintHub")
            .put("unique_id", "${NODE_ID}_notify")
            .put("command_topic", printTopic)
            .put("command_template", commandTemplate)
            .put("device", deviceBlock())
        availabilityBlock(notifyCfg)
        publish("homeassistant/notify/$NODE_ID/config", notifyCfg.toString(), retained = true)

        // Sensor entity — last job status, with format/timestamp as attributes.
        val sensorCfg = JSONObject()
            .put("name", "Sunmi PrintHub Last Job")
            .put("unique_id", "${NODE_ID}_lastjob")
            .put("state_topic", lastJobTopic)
            .put("value_template", "{{ value_json.status }}")
            .put("json_attributes_topic", lastJobTopic)
            .put("icon", "mdi:printer")
            .put("device", deviceBlock())
        availabilityBlock(sensorCfg)
        publish("homeassistant/sensor/$NODE_ID/config", sensorCfg.toString(), retained = true)
    }
}
