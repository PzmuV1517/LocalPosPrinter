package com.sunmi.printhub.net

import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Scout — reports this device's own events up to the server's /ingest (Watchtower), HMAC-signed.
 * Makes the printer one of the monitored devices in its own dashboard. Best-effort and fully
 * fire-and-forget: a reporting failure must never affect printing.
 *
 * Needs a device id + secret issued in the Watchtower dashboard ("Issue device secret").
 */
class ScoutClient(
    domain: String,
    private val deviceId: String,
    private val secret: String,
) {
    private val base: String = run {
        val d = domain.trim().removeSuffix("/")
        when {
            d.startsWith("http://") || d.startsWith("https://") -> d
            d.startsWith("ws://") -> "http://" + d.removePrefix("ws://")
            d.startsWith("wss://") -> "https://" + d.removePrefix("wss://")
            else -> "https://$d"
        }
    }

    private val client = OkHttpClient.Builder()
        .callTimeout(6, TimeUnit.SECONDS)
        .build()

    val configured: Boolean get() = deviceId.isNotBlank() && secret.isNotBlank() && base.isNotBlank()

    /** Ship one log event on a background thread; never throws. */
    fun ship(severity: String, message: String, service: String = "", meta: Map<String, Any?> = emptyMap()) {
        if (!configured) return
        Thread {
            try {
                val json = JSONObject()
                json.put("severity", severity)
                json.put("message", message)
                json.put("service", service)
                json.put("ts", System.currentTimeMillis() / 1000.0)
                if (meta.isNotEmpty()) json.put("meta", JSONObject(meta))
                val body = json.toString().toByteArray(Charsets.UTF_8)
                val headers = HmacSigner.headers(secret, deviceId, "POST", "/ingest", body)
                val req = Request.Builder()
                    .url("$base/ingest")
                    .post(body.toRequestBody("application/json".toMediaType()))
                    .apply { headers.forEach { (k, v) -> header(k, v) } }
                    .build()
                client.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) Log.w(TAG, "Scout ingest HTTP ${resp.code}")
                }
            } catch (t: Throwable) {
                Log.w(TAG, "Scout ingest failed: ${t.message}")
            }
        }.apply { isDaemon = true }.start()
    }

    companion object {
        private const val TAG = "ScoutClient"
    }
}
