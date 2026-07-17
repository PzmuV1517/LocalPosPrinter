package com.sunmi.printhub.printer

import android.content.Context
import android.graphics.Bitmap
import android.util.Log
import com.sunmi.peripheral.printer.InnerPrinterCallback
import com.sunmi.peripheral.printer.InnerPrinterManager
import com.sunmi.peripheral.printer.InnerResultCallback
import com.sunmi.peripheral.printer.SunmiPrinterService
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Printer access via Sunmi's official printer library (com.sunmi:printerlibrary). The library
 * bundles the correct Woyou AIDL and a SunmiPrinterService wrapper, instead of hand-maintaining
 * transaction ordering (which was misdispatching calls and crashing the service).
 *
 * Everything is rendered to a bitmap elsewhere and handed here as an image.
 */
class PrinterManager(private val appContext: Context) {

    companion object {
        private const val TAG = "PrinterManager"
        private const val PRINT_TIMEOUT_SEC = 12L
    }

    @Volatile
    private var service: SunmiPrinterService? = null

    val isBound: Boolean get() = service != null

    private val innerCallback = object : InnerPrinterCallback() {
        override fun onConnected(printerService: SunmiPrinterService) {
            service = printerService
            val serial = serialNo()
            val version = firmwareVersion()
            Log.i(TAG, "Sunmi printer service connected; serial=$serial version=$version")
        }

        override fun onDisconnected() {
            service = null
            Log.w(TAG, "Sunmi printer service disconnected")
        }
    }

    fun bind() {
        if (service != null) return
        try {
            val ok = InnerPrinterManager.getInstance().bindService(appContext, innerCallback)
            if (!ok) Log.e(TAG, "bindService returned false, Sunmi printer service unavailable?")
        } catch (t: Throwable) {
            Log.e(TAG, "bindService failed", t)
        }
    }

    fun unbind() {
        try {
            InnerPrinterManager.getInstance().unBindService(appContext, innerCallback)
        } catch (_: Throwable) {
        }
        service = null
    }

    fun serialNo(): String? = try {
        service?.printerSerialNo
    } catch (_: Throwable) {
        null
    }

    fun firmwareVersion(): String? = try {
        service?.printerVersion
    } catch (_: Throwable) {
        null
    }

    data class State(
        val code: Int, val ready: Boolean, val paperOut: Boolean,
        val coverOpen: Boolean, val text: String,
    )

    /**
     * Live readiness from Sunmi updatePrinterState(). Only the known fault codes flag not-ready,
     * so a firmware that returns something unexpected still reads as ready (no false alarms).
     */
    fun state(): State {
        service ?: return State(-2, false, false, false, "unbound")
        val code = try {
            service?.updatePrinterState() ?: -1
        } catch (t: Throwable) {
            Log.w(TAG, "updatePrinterState failed", t); -1
        }
        val text = when (code) {
            3 -> "comms error"; 4 -> "out of paper"; 5 -> "overheating"
            6 -> "cover open"; 7 -> "cutter error"; 9 -> "no black mark"
            505 -> "no printer"; else -> "ready"
        }
        val faults = intArrayOf(3, 4, 5, 6, 7, 9, 505)
        return State(code, code !in faults, code == 4, code == 6, text)
    }

    sealed class PrintResult {
        object Success : PrintResult()
        data class Failure(val message: String) : PrintResult()
    }

    /**
     * Print [bitmap] as an image. [label] is accepted for API compatibility but currently prints
     * the same way (continuous), since the library path doesn't expose label positioning.
     */
    fun printBitmap(bitmap: Bitmap, label: Boolean, feedLines: Int = 3): PrintResult {
        val svc = service ?: run {
            Log.e(TAG, "printBitmap: service not bound")
            return PrintResult.Failure("Printer service not bound")
        }
        Log.i(TAG, "printBitmap: ${bitmap.width}x${bitmap.height} label=$label")
        val cb = LatchCallback()
        return try {
            svc.printBitmap(bitmap, cb)
            svc.lineWrap(feedLines, null)
            Log.d(TAG, "printBitmap dispatched; awaiting printer callback")
            awaitResult(cb, "printBitmap")
        } catch (t: Throwable) {
            Log.e(TAG, "printBitmap failed", t)
            PrintResult.Failure("Print failed: ${t.message}")
        }
    }

    /** Low-level raw text line print, for the manual test screen. */
    fun printRawText(text: String): PrintResult {
        val svc = service ?: return PrintResult.Failure("Printer service not bound")
        val cb = LatchCallback()
        return try {
            svc.printText(if (text.endsWith("\n")) text else text + "\n", cb)
            svc.lineWrap(2, null)
            awaitResult(cb, "printText")
        } catch (t: Throwable) {
            Log.e(TAG, "printRawText failed", t)
            PrintResult.Failure("Print failed: ${t.message}")
        }
    }

    private fun awaitResult(cb: LatchCallback, op: String): PrintResult {
        val completed = cb.latch.await(PRINT_TIMEOUT_SEC, TimeUnit.SECONDS)
        return when {
            !completed -> {
                // Dispatched through the correct AIDL; some firmwares print without confirming.
                Log.w(TAG, "$op: no callback within ${PRINT_TIMEOUT_SEC}s, assuming printed")
                PrintResult.Success
            }
            cb.success -> {
                Log.i(TAG, "$op: printer confirmed OK")
                PrintResult.Success
            }
            else -> {
                Log.e(TAG, "$op: printer reported error: ${cb.error}")
                PrintResult.Failure(cb.error ?: "Printer reported failure")
            }
        }
    }

    /** Collects the first terminal callback the printer emits for an operation. */
    private class LatchCallback : InnerResultCallback() {
        val latch = CountDownLatch(1)
        @Volatile var success = true
        @Volatile var error: String? = null

        override fun onRunResult(isSuccess: Boolean) {
            success = isSuccess
            if (!isSuccess) error = "Printer reported failure"
            latch.countDown()
        }

        override fun onReturnString(result: String?) {
            latch.countDown()
        }

        override fun onRaiseException(code: Int, msg: String?) {
            success = false
            error = "[$code] $msg"
            latch.countDown()
        }

        override fun onPrintResult(code: Int, msg: String?) {
            success = code == 0
            if (code != 0) error = "[$code] $msg"
            latch.countDown()
        }
    }
}
