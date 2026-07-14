package com.sunmi.printhub.settings

import android.content.Context
import android.content.SharedPreferences
import kotlin.random.Random

/**
 * All app configuration, backed by SharedPreferences. Fine for a hobby LAN tool —
 * no need for anything heavier. The single access code is checked identically across
 * HTTP, MQTT and the internet listener.
 */
class Settings(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences("printhub", Context.MODE_PRIVATE)

    // ---- access password ----
    var accessPassword: String
        get() = prefs.getString(K_PASSWORD, null) ?: regenerateAccessPassword()
        set(v) = prefs.edit().putString(K_PASSWORD, v).apply()

    fun regenerateAccessPassword(): String {
        val pw = (100000 + Random.nextInt(900000)).toString()
        prefs.edit().putString(K_PASSWORD, pw).apply()
        return pw
    }

    // ---- print ----
    var printWidthPx: Int
        get() = prefs.getInt(K_WIDTH, 384)
        set(v) = prefs.edit().putInt(K_WIDTH, v).apply()

    /** "receipt" | "label" — used when a request omits print_mode. */
    var defaultPrintMode: String
        get() = prefs.getString(K_DEFAULT_MODE, "receipt") ?: "receipt"
        set(v) = prefs.edit().putString(K_DEFAULT_MODE, v).apply()

    // ---- HTTP ----
    var httpEnabled: Boolean
        get() = prefs.getBoolean(K_HTTP_ON, true)
        set(v) = prefs.edit().putBoolean(K_HTTP_ON, v).apply()

    var httpPort: Int
        get() = prefs.getInt(K_HTTP_PORT, 8080)
        set(v) = prefs.edit().putInt(K_HTTP_PORT, v).apply()

    // ---- MQTT ----
    var mqttEnabled: Boolean
        get() = prefs.getBoolean(K_MQTT_ON, false)
        set(v) = prefs.edit().putBoolean(K_MQTT_ON, v).apply()

    var mqttHost: String
        get() = prefs.getString(K_MQTT_HOST, "") ?: ""
        set(v) = prefs.edit().putString(K_MQTT_HOST, v).apply()

    var mqttPort: Int
        get() = prefs.getInt(K_MQTT_PORT, 1883)
        set(v) = prefs.edit().putInt(K_MQTT_PORT, v).apply()

    var mqttUser: String
        get() = prefs.getString(K_MQTT_USER, "") ?: ""
        set(v) = prefs.edit().putString(K_MQTT_USER, v).apply()

    var mqttPass: String
        get() = prefs.getString(K_MQTT_PASS, "") ?: ""
        set(v) = prefs.edit().putString(K_MQTT_PASS, v).apply()

    var mqttTls: Boolean
        get() = prefs.getBoolean(K_MQTT_TLS, false)
        set(v) = prefs.edit().putBoolean(K_MQTT_TLS, v).apply()

    var mqttPrefix: String
        get() = prefs.getString(K_MQTT_PREFIX, "sunmi/printhub/") ?: "sunmi/printhub/"
        set(v) = prefs.edit().putString(K_MQTT_PREFIX, normalizePrefix(v)).apply()

    // ---- internet listener ----
    var internetEnabled: Boolean
        get() = prefs.getBoolean(K_NET_ON, false)
        set(v) = prefs.edit().putBoolean(K_NET_ON, v).apply()

    var internetDomain: String
        get() = prefs.getString(K_NET_DOMAIN, "") ?: ""
        set(v) = prefs.edit().putString(K_NET_DOMAIN, v).apply()

    // ---- Watchtower / HMAC device identity ----
    // A stable per-install id (issue a matching secret in the Watchtower dashboard). When a
    // secret is set, the internet listener signs its connection with HMAC instead of putting
    // the access password in the URL.
    var deviceId: String
        get() = prefs.getString(K_DEVICE_ID, null) ?: generateDeviceId()
        set(v) = prefs.edit().putString(K_DEVICE_ID, v.trim()).apply()

    var deviceSecret: String
        get() = prefs.getString(K_DEVICE_SECRET, "") ?: ""
        set(v) = prefs.edit().putString(K_DEVICE_SECRET, v.trim()).apply()

    private fun generateDeviceId(): String {
        val id = "sunmi-" + (100000 + Random.nextInt(900000)).toString()
        prefs.edit().putString(K_DEVICE_ID, id).apply()
        return id
    }

    // ---- Confer (private chat) — token persists so the user stays logged in across restarts ----
    // The Confer server is configured separately from the internet listener: chat can live on a
    // communally-agreed Watchtower that is a different machine than the one handling prints/logs.
    // Blank falls back to the internet-listener domain (the common single-server case).
    var conferServer: String
        get() = prefs.getString(K_CONFER_SERVER, "") ?: ""
        set(v) = prefs.edit().putString(K_CONFER_SERVER, v.trim()).apply()

    /** The Confer server actually used: the explicit field, or the internet domain if unset. */
    val conferServerEffective: String
        get() = conferServer.ifBlank { internetDomain }

    var conferToken: String
        get() = prefs.getString(K_CONFER_TOKEN, "") ?: ""
        set(v) = prefs.edit().putString(K_CONFER_TOKEN, v).apply()

    var conferUsername: String
        get() = prefs.getString(K_CONFER_USER, "") ?: ""
        set(v) = prefs.edit().putString(K_CONFER_USER, v).apply()

    var conferDisplay: String
        get() = prefs.getString(K_CONFER_DISPLAY, "") ?: ""
        set(v) = prefs.edit().putString(K_CONFER_DISPLAY, v).apply()

    /** True while the user wants the printer in Confer mode (persisted so it survives restarts). */
    var conferMode: Boolean
        get() = prefs.getBoolean(K_CONFER_MODE, false)
        set(v) = prefs.edit().putBoolean(K_CONFER_MODE, v).apply()

    fun clearConfer() {
        prefs.edit().remove(K_CONFER_TOKEN).remove(K_CONFER_USER)
            .remove(K_CONFER_DISPLAY).putBoolean(K_CONFER_MODE, false).apply()
    }

    // ---- boot ----
    var autoStart: Boolean
        get() = prefs.getBoolean(K_AUTOSTART, true)
        set(v) = prefs.edit().putBoolean(K_AUTOSTART, v).apply()

    private fun normalizePrefix(v: String): String =
        if (v.isBlank()) "sunmi/printhub/" else if (v.endsWith("/")) v else "$v/"

    companion object {
        private const val K_PASSWORD = "access_password"
        private const val K_WIDTH = "print_width"
        private const val K_DEFAULT_MODE = "default_print_mode"
        private const val K_HTTP_ON = "http_enabled"
        private const val K_HTTP_PORT = "http_port"
        private const val K_MQTT_ON = "mqtt_enabled"
        private const val K_MQTT_HOST = "mqtt_host"
        private const val K_MQTT_PORT = "mqtt_port"
        private const val K_MQTT_USER = "mqtt_user"
        private const val K_MQTT_PASS = "mqtt_pass"
        private const val K_MQTT_TLS = "mqtt_tls"
        private const val K_MQTT_PREFIX = "mqtt_prefix"
        private const val K_NET_ON = "internet_enabled"
        private const val K_NET_DOMAIN = "internet_domain"
        private const val K_DEVICE_ID = "device_id"
        private const val K_DEVICE_SECRET = "device_secret"
        private const val K_CONFER_SERVER = "confer_server"
        private const val K_CONFER_TOKEN = "confer_token"
        private const val K_CONFER_USER = "confer_username"
        private const val K_CONFER_DISPLAY = "confer_display"
        private const val K_CONFER_MODE = "confer_mode"
        private const val K_AUTOSTART = "auto_start"
    }
}
