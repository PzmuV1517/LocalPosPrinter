package com.sunmi.printhub.core

import android.util.Log
import com.sunmi.printhub.db.JobSource
import com.sunmi.printhub.db.JobStatus
import com.sunmi.printhub.model.PrintPayload
import com.sunmi.printhub.printer.PrinterManager
import com.sunmi.printhub.render.ReceiptRenderer

/**
 * The single shared job pipeline: access-code check -> render -> print -> log.
 * Every channel (local UI, HTTP, MQTT, internet listener) funnels through here so
 * behaviour and logging stay identical.
 */
object PrintDispatcher {

    private const val TAG = "PrintDispatcher"

    data class Result(
        val accepted: Boolean,
        val status: JobStatus,
        val jobId: Long,
        val format: String,
        val error: String?,
    )

    /**
     * @param requirePassword local on-device prints pass false; all network channels pass true.
     * @param passwordOverride e.g. the HTTP X-Access-Password header, checked if the body has none.
     * @param sourceInfo e.g. remote IP, recorded on rejects for the log view only.
     */
    @Synchronized
    fun dispatch(
        payload: PrintPayload,
        source: JobSource,
        requirePassword: Boolean = true,
        passwordOverride: String? = null,
        sourceInfo: String? = null,
    ): Result {
        val jobLog = Hub.jobLog
        val settings = Hub.settings
        val format = payload.formatEnum.wire

        // --- auth ---
        if (requirePassword) {
            val provided = payload.effectivePassword ?: passwordOverride
            if (provided.isNullOrEmpty() || provided != settings.accessPassword) {
                val err = "Unauthorized" + (sourceInfo?.let { " from $it" } ?: "")
                val id = jobLog.insert(source, format, payload.title, payload.text, JobStatus.REJECTED, err)
                Log.w(TAG, "Rejected $source job: $err")
                return finish(Result(false, JobStatus.REJECTED, id, format, err), source)
            }
        }

        val id = jobLog.insert(source, format, payload.title, payload.text, JobStatus.QUEUED)
        jobLog.updateStatus(id, JobStatus.PRINTING)

        // --- render ---
        val bitmap = try {
            ReceiptRenderer.render(payload, settings.printWidthPx)
        } catch (t: Throwable) {
            val err = t.message ?: t.javaClass.simpleName
            jobLog.updateStatus(id, JobStatus.FAILED, "Render: $err")
            Log.e(TAG, "Render failed", t)
            return finish(Result(true, JobStatus.FAILED, id, format, err), source)
        }

        // --- print ---
        val modeWire = payload.printMode ?: settings.defaultPrintMode
        val label = modeWire.equals("label", ignoreCase = true)
        val printResult = Hub.printer.printBitmap(bitmap, label)

        val result = when (printResult) {
            is PrinterManager.PrintResult.Success -> {
                jobLog.updateStatus(id, JobStatus.SUCCESS)
                Result(true, JobStatus.SUCCESS, id, format, null)
            }
            is PrinterManager.PrintResult.Failure -> {
                jobLog.updateStatus(id, JobStatus.FAILED, printResult.message)
                Result(true, JobStatus.FAILED, id, format, printResult.message)
            }
        }
        return finish(result, source)
    }

    private fun finish(result: Result, source: JobSource): Result {
        try {
            Hub.jobCompleteListener?.invoke(result, source)
        } catch (_: Throwable) {
        }
        return result
    }

    /** Parse a JSON string and dispatch. Parse errors are logged as failed. */
    fun dispatchJson(
        json: String,
        source: JobSource,
        requirePassword: Boolean = true,
        passwordOverride: String? = null,
        sourceInfo: String? = null,
    ): Result {
        val payload = try {
            PrintPayload.parse(json)
        } catch (t: Throwable) {
            val id = Hub.jobLog.insert(source, "?", null, null, JobStatus.FAILED, "Bad JSON: ${t.message}")
            return Result(false, JobStatus.FAILED, id, "?", "Bad JSON")
        }
        return dispatch(payload, source, requirePassword = requirePassword, passwordOverride = passwordOverride, sourceInfo = sourceInfo)
    }
}
