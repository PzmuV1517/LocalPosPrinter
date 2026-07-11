package com.sunmi.printhub

import android.app.Application
import com.sunmi.printhub.core.Hub

class PrintHubApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Hub.init(this)
        // Bind the printer eagerly so manual prints work even before the service starts.
        Hub.printer.bind()
    }

    companion object {
        const val VERSION = BuildConfig.VERSION_NAME
    }
}
