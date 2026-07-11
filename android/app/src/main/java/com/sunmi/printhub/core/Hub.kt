package com.sunmi.printhub.core

import android.content.Context
import com.sunmi.printhub.db.JobLog
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

    // Live connection state for the always-on fleet (Hershey Highway) broadcast channel.
    @Volatile
    var fleetConnected = false

    /** Notified after every dispatched job (any source) so MQTT can publish lastjob, etc. */
    @Volatile
    var jobCompleteListener: ((PrintDispatcher.Result, com.sunmi.printhub.db.JobSource) -> Unit)? = null

    fun init(context: Context) {
        if (initialised) return
        val app = context.applicationContext
        settings = Settings(app)
        printer = PrinterManager(app)
        jobLog = JobLog(app)
        initialised = true
    }
}
