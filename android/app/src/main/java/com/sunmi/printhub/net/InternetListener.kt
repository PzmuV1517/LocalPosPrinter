package com.sunmi.printhub.net

import android.util.Log
import com.sunmi.printhub.core.ConferManager
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.core.PrintDispatcher
import com.sunmi.printhub.db.JobSource
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit

/**
 * Outbound WebSocket client to the Watchtower server's /messages relay. The connection is
 * authenticated with **HMAC** (device id + secret, issued in the dashboard), the secret never
 * appears in the URL. Because the channel itself is authenticated, pushed jobs are trusted and
 * printed without re-checking a per-job password.
 *
 * Reconnects with exponential backoff; this is an always-on listener.
 */
class InternetListener(
    private val domain: String,
    private val deviceId: String,
    private val deviceSecret: String,
    private val path: String = "/messages",
) {

    companion object {
        private const val TAG = "InternetListener"
        private const val MIN_BACKOFF_MS = 1_000L
        private const val MAX_BACKOFF_MS = 15_000L
    }

    // Short ping interval so a restarted/crashed server (or a proxy that silently drops the
    // upstream) is noticed within ~10s, instead of appearing offline for a while.
    private val client = OkHttpClient.Builder()
        .pingInterval(10, TimeUnit.SECONDS)
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(true)
        .build()

    @Volatile private var webSocket: WebSocket? = null
    @Volatile private var running = false
    @Volatile private var backoff = MIN_BACKOFF_MS
    private var reconnectThread: Thread? = null

    /** True only when we have the HMAC identity needed to authenticate. */
    private val ready: Boolean get() = deviceSecret.isNotBlank() && deviceId.isNotBlank() && domain.isNotBlank()

    fun start() {
        if (running) return
        if (!ready) {
            Log.w(TAG, "Not starting: no device secret configured (pair this printer in the Watchtower dashboard)")
            return
        }
        running = true
        connect()
    }

    fun stop() {
        running = false
        try {
            webSocket?.close(1000, "shutting down")
        } catch (_: Throwable) {
        }
        webSocket = null
        Hub.internetConnected = false
        reconnectThread?.interrupt()
    }

    /** Send a raw JSON frame (Confer hello / mode / read) over the socket. False if not connected. */
    fun sendFrame(json: String): Boolean = try {
        webSocket?.send(json) ?: false
    } catch (t: Throwable) {
        Log.w(TAG, "sendFrame failed: ${t.message}"); false
    }

    private fun url(): String {
        val d = domain.trim().removeSuffix("/")
        val base = when {
            d.startsWith("ws://") || d.startsWith("wss://") -> d
            d.startsWith("http://") -> "ws://" + d.removePrefix("http://")
            d.startsWith("https://") -> "wss://" + d.removePrefix("https://")
            else -> "wss://$d"
        }
        return "$base$path"
    }

    private fun connect() {
        if (!running) return
        val builder = Request.Builder().url(url())
        // Sign the upgrade GET; body is empty for a WS handshake.
        HmacSigner.headers(deviceSecret, deviceId, "GET", path).forEach { (k, v) -> builder.header(k, v) }
        val request = builder.build()
        Log.i(TAG, "Connecting to ${request.url.redact()} (HMAC as $deviceId)")
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "Internet listener connected")
                Hub.internetConnected = true
                backoff = MIN_BACKOFF_MS
                Hub.reportEvent("info", "printer connected to Watchtower", "printer.net")
                // Re-announce Confer mode after a (re)connect so chat resumes without user action.
                ConferManager.onSocketOpen()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                // This socket carries trusted (HMAC-authenticated) print jobs. Confer chat rides a
                // separate ConferSocket to the configured Confer server, not this channel.
                if (text.contains("\"format\"") || text.contains("\"image")) {
                    PrintDispatcher.dispatchJson(
                        text, JobSource.INTERNET, requirePassword = false, sourceInfo = "internet"
                    )
                } else {
                    Log.d(TAG, "Non-job frame: ${text.take(120)}")
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Hub.internetConnected = false
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Hub.internetConnected = false
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "Internet listener failure: ${t.message}")
                Hub.internetConnected = false
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (!running) return
        // Small jitter avoids every device reconnecting in lock-step after a server restart.
        val jitter = (Math.random() * 0.3 * backoff).toLong()
        val delay = backoff + jitter
        backoff = (backoff * 2).coerceAtMost(MAX_BACKOFF_MS)
        reconnectThread = Thread {
            try {
                Thread.sleep(delay)
            } catch (_: InterruptedException) {
                return@Thread
            }
            if (running) connect()
        }.also { it.isDaemon = true; it.start() }
    }
}
