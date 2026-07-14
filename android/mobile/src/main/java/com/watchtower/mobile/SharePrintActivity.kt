package com.watchtower.mobile

import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Base64
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.watchtower.mobile.databinding.ActivitySharePrintBinding
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Receives an image shared from Photos/Gallery and prints it via Watchtower's /print. */
class SharePrintActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySharePrintBinding
    private lateinit var store: Store
    private var dataUrl: String? = null
    private val client = OkHttpClient.Builder().callTimeout(20, TimeUnit.SECONDS).build()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = Store(this)
        binding = ActivitySharePrintBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val uri = streamUri()
        if (uri == null) { toast("No image shared"); finish(); return }

        val bmp = loadScaled(uri, 384)
        if (bmp == null) { binding.status.text = "Couldn't read that image."; binding.printBtn.isEnabled = false; return }
        binding.preview.setImageBitmap(bmp)
        dataUrl = "data:image/png;base64," + encodePng(bmp)

        if (store.token.isBlank()) {
            binding.status.text = "Open Watchtower and sign in first, then share again."
            binding.printBtn.isEnabled = false
        }
        binding.cancelBtn.setOnClickListener { finish() }
        binding.printBtn.setOnClickListener { doPrint() }
    }

    @Suppress("DEPRECATION")
    private fun streamUri(): Uri? {
        if (intent?.action != Intent.ACTION_SEND) return null
        return if (Build.VERSION.SDK_INT >= 33)
            intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else intent.getParcelableExtra(Intent.EXTRA_STREAM)
    }

    private fun loadScaled(uri: Uri, maxW: Int): Bitmap? = try {
        val src = contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it) }
        when {
            src == null -> null
            src.width <= maxW -> src
            else -> Bitmap.createScaledBitmap(src, maxW,
                (src.height.toFloat() * maxW / src.width).toInt().coerceAtLeast(1), true)
        }
    } catch (t: Throwable) { null }

    private fun encodePng(bmp: Bitmap): String {
        val baos = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.PNG, 100, baos)
        return Base64.encodeToString(baos.toByteArray(), Base64.NO_WRAP)
    }

    private fun doPrint() {
        val token = store.token
        val body = dataUrl
        if (token.isBlank() || body == null) { toast("Not ready"); return }
        binding.printBtn.isEnabled = false
        binding.status.text = "Printing…"
        val json = JSONObject().put("format", "image").put("image", body).put("print_mode", "receipt")
            .toString().toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url(store.serverUrl + "/print").post(json)
            .header("Authorization", "Bearer $token").build()
        client.newCall(req).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) = runOnUiThread {
                binding.status.text = "Failed: ${e.message}"; binding.printBtn.isEnabled = true
            }
            override fun onResponse(call: Call, response: Response) {
                val ok = response.isSuccessful
                val msg = try { JSONObject(response.body?.string() ?: "{}").optString(if (ok) "message" else "error") }
                catch (t: Throwable) { "" }
                runOnUiThread {
                    if (ok) { toast("Sent to printer"); finish() }
                    else { binding.status.text = "Failed: ${msg.ifBlank { "HTTP ${response.code}" }}"; binding.printBtn.isEnabled = true }
                }
            }
        })
    }

    private fun toast(m: String) = Toast.makeText(this, m, Toast.LENGTH_SHORT).show()
}
