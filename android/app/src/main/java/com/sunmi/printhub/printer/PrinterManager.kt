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
            Log.i(TAG, "Woyou printer service bound")
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
        val svc = service ?: return PrintResult.Failure("Printer service not bound")
        val cb = LatchCallback()
        return try {
            if (label) {
                try {
                    svc.labelLocate()
                } catch (e: Throwable) {
                    return PrintResult.Failure(
                        "Label mode not supported by this printer/firmware (${e.javaClass.simpleName})"
                    )
                }
            }
            svc.printBitmap(bitmap, cb)
            if (label) {
                try {
                    svc.labelOutput()
                } catch (e: Throwable) {
                    return PrintResult.Failure("Label output failed: ${e.message}")
                }
            } else {
                svc.lineWrap(feedLines, NoopCallback)
            }
            awaitResult(cb)
        } catch (e: RemoteException) {
            PrintResult.Failure("Printer RemoteException: ${e.message}")
        } catch (t: Throwable) {
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
            awaitResult(cb)
        } catch (t: Throwable) {
            PrintResult.Failure("Print failed: ${t.message}")
        }
    }

    private fun awaitResult(cb: LatchCallback): PrintResult {
        val completed = cb.latch.await(PRINT_TIMEOUT_SEC, TimeUnit.SECONDS)
        return when {
            !completed -> PrintResult.Success // Fire-and-forget: printer didn't call back in time.
            cb.success -> PrintResult.Success
            else -> PrintResult.Failure(cb.error ?: "Printer reported failure")
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
