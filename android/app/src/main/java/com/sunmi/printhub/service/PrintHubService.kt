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
 * limits, so a plain persistent foreground Service is enough, no JobScheduler workarounds.
 */
class PrintHubService : Service() {

    companion object {
        private const val TAG = "PrintHubService"
        private const val CHANNEL_ID = "printhub_service"
        private const val NOTIF_ID = 1001
        // Doze-proof heartbeat: a self-rescheduling exact alarm that wakes the app to re-acquire
        // locks + reconnect, so a screen-off backlog flushes within ~a minute instead of waiting
        // for the screen to come on. When the app is battery-whitelisted this fires on time; even
        // if it isn't, it still fires during Doze maintenance windows.
        private const val ACTION_HEARTBEAT = "com.sunmi.printhub.HEARTBEAT"
        private const val HEARTBEAT_INTERVAL_MS = 30_000L

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
        if (intent?.action == ACTION_HEARTBEAT) {
            heartbeat()
        } else {
            startChannels()
        }
        scheduleHeartbeat()
        return START_STICKY
    }

    /**
     * Fired by the alarm. Waking the CPU here lets any socket data that Doze deferred get
     * processed (that flushes the backlog), and re-acquires locks / forces a reconnect
     * if the link actually dropped, all without needing the screen to come on.
     */
    private fun heartbeat() {
        if (wakeLock?.isHeld != true || wifiLock?.isHeld != true) {
            releaseLocks(); acquireLocks()
        }
        if (!Hub.internetConnected && Hub.settings.internetEnabled && Hub.settings.internetDomain.isNotBlank()) {
            Log.i(TAG, "Heartbeat: internet link down, reconnecting")
            stopInternet()
            internet = InternetListener(
                Hub.settings.internetDomain, deviceId = Hub.settings.deviceId,
                deviceSecret = Hub.settings.deviceSecret,
            ).also { it.start() }
            Hub.internet = internet
        }
    }

    private fun scheduleHeartbeat() {
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        else PendingIntent.FLAG_UPDATE_CURRENT
        val fire = PendingIntent.getService(
            this, 2, Intent(this, PrintHubService::class.java).setAction(ACTION_HEARTBEAT), flags)
        try {
            val am = getSystemService(Context.ALARM_SERVICE) as android.app.AlarmManager
            val next = System.currentTimeMillis() + HEARTBEAT_INTERVAL_MS
            // Alarm-CLOCK alarms are exempt from Doze and fire at the exact time even in deep Doze,
            // with no battery-optimization whitelist and on battery. That's what lets the listener
            // wake and flush pushed jobs while the screen is off. (A user-visible next-alarm icon is
            // the accepted trade-off for a dedicated always-on POS hub.)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                val show = PendingIntent.getActivity(
                    this, 3, Intent(this, MainActivity::class.java), flags)
                am.setAlarmClock(android.app.AlarmManager.AlarmClockInfo(next, show), fire)
            } else {
                am.setExact(android.app.AlarmManager.RTC_WAKEUP, next, fire)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "Failed to schedule heartbeat", t)
            // Fall back to the best Doze-tolerant option if setAlarmClock is unavailable.
            try {
                val am = getSystemService(Context.ALARM_SERVICE) as android.app.AlarmManager
                am.setExactAndAllowWhileIdle(android.app.AlarmManager.RTC_WAKEUP,
                    System.currentTimeMillis() + HEARTBEAT_INTERVAL_MS, fire)
            } catch (_: Throwable) {}
        }
    }

    private fun cancelHeartbeat() {
        val pi = PendingIntent.getService(
            this, 2, Intent(this, PrintHubService::class.java).setAction(ACTION_HEARTBEAT),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
            else PendingIntent.FLAG_UPDATE_CURRENT
        )
        try { (getSystemService(Context.ALARM_SERVICE) as android.app.AlarmManager).cancel(pi) } catch (_: Throwable) {}
    }

    /**
     * The user swiped the app off the recent-apps list. On stock Android a foreground service
     * survives this, but aggressive OEM ROMs (Sunmi included) tear the whole process down anyway.
     * START_STICKY alone can leave a multi-minute gap, so it also schedules an almost-immediate
     * restart via AlarmManager: whichever path wins, the print listener is never down for long.
     */
    override fun onTaskRemoved(rootIntent: Intent?) {
        val restart = Intent(applicationContext, PrintHubService::class.java)
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        else PendingIntent.FLAG_UPDATE_CURRENT
        val pi = PendingIntent.getService(this, 1, restart, flags)
        try {
            val am = getSystemService(Context.ALARM_SERVICE) as android.app.AlarmManager
            am.set(android.app.AlarmManager.RTC_WAKEUP, System.currentTimeMillis() + 1000, pi)
        } catch (t: Throwable) {
            Log.e(TAG, "Failed to schedule restart after task removal", t)
        }
        super.onTaskRemoved(rootIntent)
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

        // Internet listener (HMAC, needs a device secret paired in the Watchtower dashboard).
        stopInternet()
        if (s.internetEnabled && s.internetDomain.isNotBlank()) {
            internet = InternetListener(
                s.internetDomain, deviceId = s.deviceId, deviceSecret = s.deviceSecret,
            ).also { it.start() }
            // Expose it so Confer can send hello/mode/read frames over the same channel.
            Hub.internet = internet
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
        if (Hub.internet === internet) Hub.internet = null
        internet = null
    }

    override fun onDestroy() {
        cancelHeartbeat()
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
