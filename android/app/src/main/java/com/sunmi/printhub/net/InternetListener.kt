package com.sunmi.printhub.net

import android.util.Log
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
 * Outbound WebSocket client to the companion server's /messages relay. Connects to
 * wss://<domain>/messages?password=<access-password>, then sits and listens for pushed jobs.
 * Reconnects with exponential backoff — this is an always-on listener, not something
 * to babysit.
 */
class InternetListener(
    private val domain: String,
    private val accessPassword: String,
) {

    companion object {
        private const val TAG = "InternetListener"
        private const val MIN_BACKOFF_MS = 2_000L
        private const val MAX_BACKOFF_MS = 60_000L
    }

    private val client = OkHttpClient.Builder()
        .pingInterval(25, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    @Volatile private var webSocket: WebSocket? = null
    @Volatile private var running = false
    @Volatile private var backoff = MIN_BACKOFF_MS
    private var reconnectThread: Thread? = null

    fun start() {
        if (running) return
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

    private fun url(): String {
        val d = domain.trim().removeSuffix("/")
        val base = when {
            d.startsWith("ws://") || d.startsWith("wss://") -> d
            d.startsWith("http://") -> "ws://" + d.removePrefix("http://")
            d.startsWith("https://") -> "wss://" + d.removePrefix("https://")
            else -> "wss://$d"
        }
        return "$base/messages?password=$accessPassword"
    }

    private fun connect() {
        if (!running) return
        val request = Request.Builder().url(url()).build()
        Log.i(TAG, "Connecting to ${request.url.redact()}")
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "Internet listener connected")
                Hub.internetConnected = true
                backoff = MIN_BACKOFF_MS
                // Also send an auth frame, so servers that prefer a frame over the query param work.
                webSocket.send("""{"type":"auth","password":"$accessPassword"}""")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                // Ignore server control frames; only dispatch things that look like jobs.
                if (text.contains("\"format\"") || text.contains("\"image")) {
                    PrintDispatcher.dispatchJson(text, JobSource.INTERNET, sourceInfo = "internet")
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
        val delay = backoff
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
