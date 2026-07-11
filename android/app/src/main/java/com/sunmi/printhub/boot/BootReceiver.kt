package com.sunmi.printhub.boot

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.service.PrintHubService

/** Starts the print-hub service at boot when auto-start is enabled. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: return
        if (action == Intent.ACTION_BOOT_COMPLETED || action == "android.intent.action.QUICKBOOT_POWERON") {
            Hub.init(context)
            if (Hub.settings.autoStart) {
                PrintHubService.start(context)
            }
        }
    }
}
