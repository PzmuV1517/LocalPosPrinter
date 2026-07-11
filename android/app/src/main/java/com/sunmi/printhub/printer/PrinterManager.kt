package com.sunmi.printhub.printer

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.graphics.Bitmap
import android.os.IBinder
import android.os.RemoteException
import android.util.Log
import woyou.aidlservice.jiuiv5.ICallback
import woyou.aidlservice.jiuiv5.IWoyouService
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Binds to Sunmi's classic Woyou AIDL printer service and exposes a small, blocking
 * print surface. Everything is rendered to a bitmap elsewhere and handed here as an image.
 */
class PrinterManager(private val appContext: Context) {

    companion object {
        private const val TAG = "PrinterManager"
        private const val SERVICE_PACKAGE = "woyou.aidlservice.jiuiv5"
        private const val SERVICE_ACTION = "woyou.aidlservice.jiuiv5.IWoyouService"
        private const val PRINT_TIMEOUT_SEC = 12L
    }

    @Volatile
    private var service: IWoyouService? = null

    val isBound: Boolean get() = service != null

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            service = IWoyouService.Stub.asInterface(binder)
            // Reading serial/version/modal exercises AIDL methods 3/4/5. Real values back =
            // the AIDL method order is aligned with the on-device service (so printBitmap,
            // method 16, should also dispatch correctly). Nulls/exceptions = misaligned AIDL.
            val serial = serialNo()
            val version = firmwareVersion()
            val modal = printerModal()
            Log.i(TAG, "Woyou printer service bound; serial=$serial version=$version modal=$modal")
            if (serial.isNullOrBlank() && version.isNullOrBlank()) {
                Log.e(TAG, "Bound but serial+version are empty — AIDL likely misaligned with this firmware")
            }
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service = null
            Log.w(TAG, "Woyou printer service disconnected")
        }
    }

    fun bind() {
        if (service != null) return
        val intent = Intent().apply {
            setPackage(SERVICE_PACKAGE)
            action = SERVICE_ACTION
        }
        val ok = try {
            appContext.bindService(intent, connection, Context.BIND_AUTO_CREATE)
        } catch (t: Throwable) {
            Log.e(TAG, "bindService failed", t)
            false
        }
        if (!ok) Log.e(TAG, "Could not bind to Woyou service — is this a Sunmi device?")
    }

    fun unbind() {
        try {
            appContext.unbindService(connection)
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

    fun printerModal(): String? = try {
        service?.printerModal
    } catch (_: Throwable) {
        null
    }

    sealed class PrintResult {
        object Success : PrintResult()
        data class Failure(val message: String) : PrintResult()
    }

    /**
     * Print [bitmap] as an image. When [label] is true, uses the Woyou label-positioning
     * calls; if the firmware lacks label support the job fails with a descriptive error
     * rather than printing garbage.
     */
    fun printBitmap(bitmap: Bitmap, label: Boolean, feedLines: Int = 3): PrintResult {
        val svc = service ?: run {
            Log.e(TAG, "printBitmap: service not bound")
            return PrintResult.Failure("Printer service not bound")
        }
        Log.i(TAG, "printBitmap: ${bitmap.width}x${bitmap.height} label=$label")
        val cb = LatchCallback()
        return try {
            // Reset the printer first — clears any stuck state (e.g. a buffer transaction left
            // open by a previous force-close), which otherwise swallows prints with no callback.
            try {
                svc.printerInit(NoopCallback)
            } catch (t: Throwable) {
                Log.w(TAG, "printerInit failed: ${t.message}")
            }

            if (label) {
                try {
                    svc.labelLocate()
                } catch (e: Throwable) {
                    Log.e(TAG, "labelLocate failed", e)
                    return PrintResult.Failure(
                        "Label mode not supported by this printer/firmware (${e.javaClass.simpleName})"
                    )
                }
                svc.printBitmap(bitmap, cb)
                try {
                    svc.labelOutput()
                } catch (e: Throwable) {
                    Log.e(TAG, "labelOutput failed", e)
                    return PrintResult.Failure("Label output failed: ${e.message}")
                }
            } else {
                // Receipt: print inside a fresh buffer transaction and COMMIT it, so the content
                // actually flushes to the head. A bare printBitmap() can just buffer and never
                // print (accepted, no callback) on this firmware.
                svc.enterPrinterBuffer(true)   // clean=true wipes any stuck buffer
                svc.printBitmap(bitmap, cb)
                svc.lineWrap(feedLines, NoopCallback)
                svc.exitPrinterBuffer(true)    // commit=true -> print the buffered content
                Log.d(TAG, "printBitmap: buffer committed; awaiting printer callback")
            }
            awaitResult(cb, "printBitmap")
        } catch (e: RemoteException) {
            Log.e(TAG, "printBitmap RemoteException", e)
            PrintResult.Failure("Printer RemoteException: ${e.message}")
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
            svc.lineWrap(2, NoopCallback)
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
                // No callback = the printer never confirmed. This is what a wrong AIDL
                // transaction looks like (the call is silently ignored). Report it honestly
                // instead of pretending success.
                Log.e(TAG, "$op: no printer callback within ${PRINT_TIMEOUT_SEC}s — reporting FAILED")
                PrintResult.Failure("Printer did not confirm within ${PRINT_TIMEOUT_SEC}s (no callback — nothing printed?)")
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
    private class LatchCallback : ICallback.Stub() {
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

    private object NoopCallback : ICallback.Stub() {
        override fun onRunResult(isSuccess: Boolean) {}
        override fun onReturnString(result: String?) {}
        override fun onRaiseException(code: Int, msg: String?) {}
        override fun onPrintResult(code: Int, msg: String?) {}
    }
}
