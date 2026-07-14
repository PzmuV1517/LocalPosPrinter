package com.sunmi.printhub.core

import android.content.Context
import com.sunmi.printhub.db.JobLog
import com.sunmi.printhub.net.ScoutClient
import com.sunmi.printhub.printer.PrinterManager
import com.sunmi.printhub.settings.Settings

/**
 * Process-wide holder for the shared singletons the network threads and UI all touch.
 * Initialised once from [com.sunmi.printhub.PrintHubApp].
 */
object Hub {
    lateinit var settings: Settings
        private set
    lateinit var printer: PrinterManager
        private set
    lateinit var jobLog: JobLog
        private set

    @Volatile
    var initialised = false
        private set

    // Live connection state for the internet listener, surfaced in the UI.
    @Volatile
    var internetConnected = false

    // The active internet WebSocket listener (owned by the service). Confer sends its frames
    // (hello / mode / read) through this same authenticated channel.
    @Volatile
    var internet: com.sunmi.printhub.net.InternetListener? = null

    /** Notified after every dispatched job (any source) so MQTT can publish lastjob, etc. */
    @Volatile
    var jobCompleteListener: ((PrintDispatcher.Result, com.sunmi.printhub.db.JobSource) -> Unit)? = null

    // Cached Scout client for self-reporting to Watchtower; rebuilt when its config changes.
    @Volatile private var scoutClient: ScoutClient? = null
    @Volatile private var scoutKey: String = ""

    fun init(context: Context) {
        if (initialised) return
        val app = context.applicationContext
        settings = Settings(app)
        printer = PrinterManager(app)
        jobLog = JobLog(app)
        ConferManager.init(app)
        initialised = true
    }

    /** Best-effort self-report to Watchtower's /ingest. Always on once a device secret is
     *  configured (HMAC identity). Never throws (ScoutClient is fire-and-forget). */
    fun reportEvent(severity: String, message: String, service: String, noPrint: Boolean = true) {
        if (!initialised) return
        val domain = settings.internetDomain
        if (domain.isBlank() || settings.deviceSecret.isBlank()) return
        val key = "$domain|${settings.deviceId}|${settings.deviceSecret}"
        var c = scoutClient
        if (c == null || scoutKey != key) {
            c = ScoutClient(domain, settings.deviceId, settings.deviceSecret)
            scoutClient = c
            scoutKey = key
        }
        c.ship(severity, message, service, noPrint = noPrint)
    }
}
