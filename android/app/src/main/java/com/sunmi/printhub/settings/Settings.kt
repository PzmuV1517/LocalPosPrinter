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

    // ---- access code ----
    var accessCode: String
        get() = prefs.getString(K_CODE, null) ?: regenerateAccessCode()
        set(v) = prefs.edit().putString(K_CODE, v).apply()

    fun regenerateAccessCode(): String {
        val code = (100000 + Random.nextInt(900000)).toString()
        prefs.edit().putString(K_CODE, code).apply()
        return code
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

    // ---- boot ----
    var autoStart: Boolean
        get() = prefs.getBoolean(K_AUTOSTART, true)
        set(v) = prefs.edit().putBoolean(K_AUTOSTART, v).apply()

    private fun normalizePrefix(v: String): String =
        if (v.isBlank()) "sunmi/printhub/" else if (v.endsWith("/")) v else "$v/"

    companion object {
        private const val K_CODE = "access_code"
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
        private const val K_AUTOSTART = "auto_start"
    }
}
