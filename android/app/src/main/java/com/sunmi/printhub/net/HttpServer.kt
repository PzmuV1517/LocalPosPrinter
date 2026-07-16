package com.sunmi.printhub.net

import android.util.Log
import com.sunmi.printhub.BuildConfig
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.core.PrintDispatcher
import com.sunmi.printhub.db.JobSource
import com.sunmi.printhub.db.JobStatus
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response.Status
import org.json.JSONArray
import org.json.JSONObject

/**
 * Embedded LAN HTTP server. Cleartext HTTP is intentional, the target device is
 * Android 7.1 (API 25) where cleartext is allowed by default; see README.
 *
 *  POST /print    JSON body, password via X-Access-Password header or "password" field.
 *  GET  /status   health check, no password required.
 *  GET  /formats   self-describing schema, no password required.
 */
class HttpServer(port: Int) : NanoHTTPD(port) {

    companion object {
        private const val TAG = "HttpServer"
        private const val MAX_BODY_BYTES = 2 * 1024 * 1024 // 2 MB, keep base64 images sane.
    }

    override fun serve(session: IHTTPSession): Response {
        return try {
            when {
                session.method == Method.POST && session.uri == "/print" -> handlePrint(session)
                session.method == Method.GET && session.uri == "/status" -> handleStatus()
                session.method == Method.GET && session.uri == "/formats" -> handleFormats()
                else -> json(Response.Status.NOT_FOUND, errorBody("Not found"))
            }
        } catch (t: Throwable) {
            Log.e(TAG, "serve error", t)
            json(Response.Status.INTERNAL_ERROR, errorBody(t.message ?: "error"))
        }
    }

    private fun handlePrint(session: IHTTPSession): Response {
        val lenHeader = session.headers["content-length"]?.toIntOrNull() ?: 0
        if (lenHeader > MAX_BODY_BYTES) {
            // NanoHTTPD 2.3.1 has no 413 enum value; report it as a 400 with a clear message.
            return json(Status.BAD_REQUEST, errorBody("Payload too large (max ${MAX_BODY_BYTES} bytes)"))
        }

        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = files["postData"] ?: ""
        if (body.isBlank()) return json(Response.Status.BAD_REQUEST, errorBody("Empty body"))

        // Prefer X-Access-Password; still accept the legacy X-Access-Code header.
        val headerPassword = session.headers["x-access-password"] ?: session.headers["x-access-code"]
        val ip = session.headers["http-client-ip"] ?: session.remoteIpAddress

        val result = PrintDispatcher.dispatchJson(
            body, JobSource.HTTP, passwordOverride = headerPassword, sourceInfo = ip
        )

        val status = when {
            result.status == JobStatus.REJECTED -> Response.Status.UNAUTHORIZED
            !result.accepted -> Response.Status.BAD_REQUEST
            else -> Response.Status.OK
        }
        val obj = JSONObject()
            .put("ok", result.status == JobStatus.SUCCESS)
            .put("status", result.status.wire)
            .put("job_id", result.jobId)
            .put("format", result.format)
        result.error?.let { obj.put("error", it) }
        return json(status, obj.toString())
    }

    private fun handleStatus(): Response {
        val last = Hub.jobLog.recent(1).firstOrNull()
        val obj = JSONObject()
            .put("version", BuildConfig.VERSION_NAME)
            .put("queue_depth", 0)
            .put("printer_bound", Hub.printer.isBound)
            .put("internet_connected", Hub.internetConnected)
        if (last != null) {
            obj.put(
                "last_job",
                JSONObject()
                    .put("status", last.status.wire)
                    .put("format", last.format)
                    .put("source", last.source.wire)
                    .put("timestamp", last.timestamp)
                    .apply { last.error?.let { put("error", it) } }
            )
        }
        return json(Response.Status.OK, obj.toString())
    }

    private fun handleFormats(): Response {
        val formats = JSONArray()
        fun entry(name: String, fields: List<String>, note: String) {
            formats.put(
                JSONObject()
                    .put("format", name)
                    .put("fields", JSONArray(fields))
                    .put("note", note)
            )
        }
        entry("plain", listOf("text", "text_size?", "image?"), "Left-aligned text.")
        entry("centered", listOf("text", "text_size?", "image?"), "Centered text.")
        entry("boxed", listOf("text", "text_size?", "border_style?", "image?"), "Bordered box; border_style defaults to a drawn line.")
        entry("header_body", listOf("title", "text", "text_size?", "image?"), "Large centered title, divider, body.")
        entry("banner", listOf("title|text"), "Big auto-scaled centered alert text.")
        entry("list", listOf("title?", "items[{label,value}]", "text_size?"), "Two-column label/value rows.")
        entry("image", listOf("image|image_raw_bitmap"), "Pure image print, full width.")
        entry("barcode", listOf("text", "barcode_type"), "CODE128, EAN13, UPC_A, CODE39, ITF, …")
        entry("qrcode", listOf("text"), "QR code auto-scaled to width.")
        entry(
            "alert",
            listOf("alert_type", "text|message", "service", "sent_at?", "alert_font?"),
            "MUIE envelope: ALERT / type / message / service + times. alert_type: " +
                "emerg, alert, crit, err, warning, notice, info, debug. " +
                "alert_font: 1=mono (default), 2=Jersey10, 3=Jacquard12, 4=Doto.",
        )

        val borderStyles = listOf(
            "line", "dashes", "equals", "asterisk", "at", "hash",
            "dot", "plus", "wave", "box", "double", "rounded"
        )
        val obj = JSONObject()
            .put("formats", formats)
            .put("print_modes", JSONArray(listOf("receipt", "label")))
            .put("image_positions", JSONArray(listOf("top", "bottom")))
            .put("border_styles", JSONArray(borderStyles))
            .put("inline_tags", JSONArray(listOf("@#@divider=\"<pattern>\"", "@#@cats")))
            .put("auth", "X-Access-Password header or 'password' field")
        return json(Response.Status.OK, obj.toString())
    }

    private fun json(status: Status, body: String): Response =
        NanoHTTPD.newFixedLengthResponse(status, "application/json", body)

    private fun errorBody(msg: String): String = JSONObject().put("error", msg).toString()
}
