package com.sunmi.printhub.render

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.util.Base64

/**
 * Image decode / scale / dither helpers for the print pipeline.
 * Everything the printer sees ends up as pure black/white (0xFF000000 / 0xFFFFFFFF).
 */
object ImageUtils {

    fun decodeBase64(data: String): Bitmap? {
        val cleaned = stripDataUri(data)
        return try {
            val bytes = Base64.decode(cleaned, Base64.DEFAULT)
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        } catch (t: Throwable) {
            null
        }
    }

    private fun stripDataUri(data: String): String {
        val idx = data.indexOf("base64,")
        return if (idx >= 0) data.substring(idx + "base64,".length) else data
    }

    /** Scale to exactly [targetWidth] preserving aspect ratio (only downscaling if wider). */
    fun scaleToWidth(src: Bitmap, targetWidth: Int): Bitmap {
        if (src.width == targetWidth) return src
        val ratio = targetWidth.toFloat() / src.width.toFloat()
        val h = Math.max(1, Math.round(src.height * ratio))
        return Bitmap.createScaledBitmap(src, targetWidth, h, true)
    }

    /**
     * Floyd–Steinberg dither to 1-bit black/white. Produces noticeably better photos on
     * thermal heads than a flat threshold. Returns an ARGB_8888 bitmap of pure B/W pixels.
     */
    fun floydSteinberg(src: Bitmap): Bitmap {
        val w = src.width
        val h = src.height
        val out = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)

        // Grayscale buffer we can push error into.
        val gray = FloatArray(w * h)
        val pixels = IntArray(w * h)
        src.getPixels(pixels, 0, w, 0, 0, w, h)
        for (i in pixels.indices) {
            val c = pixels[i]
            val a = Color.alpha(c)
            // Treat transparent as white paper.
            val lum = if (a < 128) 255f else
                0.299f * Color.red(c) + 0.587f * Color.green(c) + 0.114f * Color.blue(c)
            gray[i] = lum
        }

        for (y in 0 until h) {
            for (x in 0 until w) {
                val idx = y * w + x
                val oldVal = gray[idx]
                val newVal = if (oldVal < 128f) 0f else 255f
                val err = oldVal - newVal
                pixels[idx] = if (newVal == 0f) 0xFF000000.toInt() else 0xFFFFFFFF.toInt()
                // Distribute error.
                if (x + 1 < w) gray[idx + 1] += err * 7f / 16f
                if (y + 1 < h) {
                    if (x > 0) gray[idx + w - 1] += err * 3f / 16f
                    gray[idx + w] += err * 5f / 16f
                    if (x + 1 < w) gray[idx + w + 1] += err * 1f / 16f
                }
            }
        }
        out.setPixels(pixels, 0, w, 0, 0, w, h)
        return out
    }

    /** Flat-threshold monochrome (used for raw-ish content where dithering is unwanted). */
    fun threshold(src: Bitmap, cutoff: Int = 128): Bitmap {
        val w = src.width
        val h = src.height
        val pixels = IntArray(w * h)
        src.getPixels(pixels, 0, w, 0, 0, w, h)
        for (i in pixels.indices) {
            val c = pixels[i]
            val lum = 0.299 * Color.red(c) + 0.587 * Color.green(c) + 0.114 * Color.blue(c)
            pixels[i] = if (lum < cutoff) 0xFF000000.toInt() else 0xFFFFFFFF.toInt()
        }
        val out = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        out.setPixels(pixels, 0, w, 0, 0, w, h)
        return out
    }
}
