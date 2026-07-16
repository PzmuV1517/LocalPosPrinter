package com.sunmi.printhub.net

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Dedicated WebSocket to the **Confer server** (which may differ from the print/internet-listener
 * server). Authenticated by the Confer participant token in the query string, so it needs no HMAC
 * device identity, a printer can chat on a communally-agreed server it doesn't print through.
 *
 * Receives live messages + offline catch-up; sends read receipts. Reconnects with backoff while
 * Confer mode is on.
 */
class ConferSocket(
    private val server: String,
    private val token: String,
    private val onFrame: (JSONObject) -> Unit,
    private val onConnected: (Boolean) -> Unit,
    private val onAuthFailed: () -> Unit,
) {
    companion object {
        private const val TAG = "ConferSocket"
        private const val MIN_BACKOFF_MS = 1_000L
        private const val MAX_BACKOFF_MS = 15_000L
    }

    private val client = OkHttpClient.Builder()
        .pingInterval(10, TimeUnit.SECONDS)
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(true)
        .build()

    @Volatile private var ws: WebSocket? = null
    @Volatile private var running = false
    @Volatile private var backoff = MIN_BACKOFF_MS
    private var reconnectThread: Thread? = null

    private fun url(): String {
        val d = server.trim().removeSuffix("/")
        val base = when {
            d.startsWith("ws://") || d.startsWith("wss://") -> d
            d.startsWith("http://") -> "ws://" + d.removePrefix("http://")
            d.startsWith("https://") -> "wss://" + d.removePrefix("https://")
            else -> "wss://$d"
        }
        return "$base/confer/ws?token=$token"
    }

    fun start() {
        if (running || server.isBlank() || token.isBlank()) return
        running = true
        connect()
    }

    fun stop() {
        running = false
        try { ws?.close(1000, "leaving confer mode") } catch (_: Throwable) {}
        ws = null
        reconnectThread?.interrupt()
        onConnected(false)
    }

    fun send(json: String): Boolean = try { ws?.send(json) ?: false } catch (_: Throwable) { false }

    private fun connect() {
        if (!running) return
        val req = Request.Builder().url(url()).build()
        Log.i(TAG, "Connecting to Confer server ${req.url.redact()}")
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "Confer socket connected"); backoff = MIN_BACKOFF_MS; onConnected(true)
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                val frame = try { JSONObject(text) } catch (_: Throwable) { return }
                onFrame(frame)
            }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                onConnected(false)
                if (code == 4401) { running = false; onAuthFailed() }
                webSocket.close(1000, null)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onConnected(false)
                if (code == 4401) { running = false; onAuthFailed() } else scheduleReconnect()
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "Confer socket failure: ${t.message}")
                onConnected(false)
                if (response?.code == 401 || response?.code == 4401) { running = false; onAuthFailed() }
                else scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (!running) return
        val jitter = (Math.random() * 0.3 * backoff).toLong()
        val delay = backoff + jitter
        backoff = (backoff * 2).coerceAtMost(MAX_BACKOFF_MS)
        reconnectThread = Thread {
            try { Thread.sleep(delay) } catch (_: InterruptedException) { return@Thread }
            if (running) connect()
        }.also { it.isDaemon = true; it.start() }
    }
}
