package com.sunmi.printhub.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import com.sunmi.printhub.R
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.net.HttpServer
import com.sunmi.printhub.net.InternetListener
import com.sunmi.printhub.net.MqttManager
import com.sunmi.printhub.ui.MainActivity

/**
 * Foreground service that hosts the HTTP server + MQTT client + internet WebSocket
 * listener and keeps the printer bound. At API 25 there are no Doze / background-execution
 * limits, so a plain persistent foreground Service is enough — no JobScheduler workarounds.
 */
class PrintHubService : Service() {

    companion object {
        private const val TAG = "PrintHubService"
        private const val CHANNEL_ID = "printhub_service"
        private const val NOTIF_ID = 1001

        fun start(context: Context) {
            val i = Intent(context, PrintHubService::class.java)
            context.startService(i)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, PrintHubService::class.java))
        }

        /** Restart the channels to pick up changed settings. */
        fun restart(context: Context) {
            stop(context)
            start(context)
        }
    }

    private var httpServer: HttpServer? = null
    private var mqtt: MqttManager? = null
    private var internet: InternetListener? = null

    // Held for the whole service lifetime so the network listeners stay connected and jobs
    // still print while the screen is off / the device is locked: the partial wake lock keeps
    // the CPU alive to service incoming sockets, and the high-perf Wi-Fi lock stops the radio
    // from powering down between packets. This is a mains-powered POS hub, so holding these
    // continuously is the intended trade-off.
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onCreate() {
        super.onCreate()
        Hub.init(this)
        Hub.printer.bind()
        acquireLocks()
        startForeground(NOTIF_ID, buildNotification())
    }

    private fun acquireLocks() {
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "printhub:cpu").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (t: Throwable) {
            Log.e(TAG, "Failed to acquire wake lock", t)
        }
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "printhub:wifi").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (t: Throwable) {
            Log.e(TAG, "Failed to acquire wifi lock", t)
        }
    }

    private fun releaseLocks() {
        try {
            wakeLock?.takeIf { it.isHeld }?.release()
        } catch (_: Throwable) {
        }
        wakeLock = null
        try {
            wifiLock?.takeIf { it.isHeld }?.release()
        } catch (_: Throwable) {
        }
        wifiLock = null
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startChannels()
        return START_STICKY
    }

    private fun startChannels() {
        val s = Hub.settings

        // HTTP
        stopHttp()
        if (s.httpEnabled) {
            try {
                httpServer = HttpServer(s.httpPort).also {
                    it.start(NanoTimeoutMs, false)
                }
                Log.i(TAG, "HTTP server on :${s.httpPort}")
            } catch (t: Throwable) {
                Log.e(TAG, "HTTP server failed to start", t)
            }
        }

        // MQTT
        stopMqtt()
        if (s.mqttEnabled) {
            mqtt = MqttManager(this).also { it.start() }
        }

        // Internet listener
        stopInternet()
        if (s.internetEnabled && s.internetDomain.isNotBlank()) {
            internet = InternetListener(s.internetDomain, s.accessPassword).also { it.start() }
        }
    }

    private fun stopHttp() {
        try {
            httpServer?.stop()
        } catch (_: Throwable) {
        }
        httpServer = null
    }

    private fun stopMqtt() {
        try {
            mqtt?.stop()
        } catch (_: Throwable) {
        }
        mqtt = null
    }

    private fun stopInternet() {
        try {
            internet?.stop()
        } catch (_: Throwable) {
        }
        internet = null
    }

    override fun onDestroy() {
        stopHttp()
        stopMqtt()
        stopInternet()
        releaseLocks()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                nm.createNotificationChannel(
                    NotificationChannel(
                        CHANNEL_ID,
                        getString(R.string.notif_channel_name),
                        NotificationManager.IMPORTANCE_LOW
                    )
                )
            }
        }
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            else PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notif_running))
            .setSmallIcon(R.drawable.ic_launcher)
            .setOngoing(true)
            .setContentIntent(pi)
            .build()
    }
}

// NanoHTTPD's start(timeout, daemon): give sockets a sane read timeout.
private const val NanoTimeoutMs = 10_000
