package com.sunmi.printhub

import android.app.Application
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.render.ReceiptRenderer

class PrintHubApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Hub.init(this)
        // Load custom alert fonts (assets/fonts/) so the renderer can use them by number.
        ReceiptRenderer.loadFonts(this)
        // Bind the printer eagerly so manual prints work even before the service starts.
        Hub.printer.bind()
    }

    companion object {
        const val VERSION = BuildConfig.VERSION_NAME
    }
}
