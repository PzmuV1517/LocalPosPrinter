package com.watchtower.mobile

import android.content.Context

/** Server URL + the web session token (mirrored from the WebView's localStorage for share-print). */
class Store(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("watchtower", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString("server_url", DEFAULT_URL) ?: DEFAULT_URL
        set(v) = prefs.edit().putString("server_url", normalize(v)).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(v) = prefs.edit().putString("token", v).apply()

    private fun normalize(v: String): String {
        var s = v.trim().removeSuffix("/")
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "https://$s"
        return s
    }

    companion object {
        const val DEFAULT_URL = "https://watchtower.andreibanu.com"
    }
}
