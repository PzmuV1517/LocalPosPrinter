package com.sunmi.printhub.render

import android.graphics.Bitmap
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.MultiFormatWriter
import com.google.zxing.common.BitMatrix
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel

/** Barcode + QR generation to Bitmap via ZXing. */
object CodeRenderer {

    class UnsupportedBarcode(type: String?) :
        IllegalArgumentException("Unsupported or missing barcode_type: $type")

    private fun mapType(type: String?): BarcodeFormat = when (type?.trim()?.uppercase()) {
        "CODE128", "CODE_128" -> BarcodeFormat.CODE_128
        "CODE39", "CODE_39" -> BarcodeFormat.CODE_39
        "EAN13", "EAN_13" -> BarcodeFormat.EAN_13
        "EAN8", "EAN_8" -> BarcodeFormat.EAN_8
        "UPC_A", "UPCA" -> BarcodeFormat.UPC_A
        "UPC_E", "UPCE" -> BarcodeFormat.UPC_E
        "ITF" -> BarcodeFormat.ITF
        "CODABAR" -> BarcodeFormat.CODABAR
        else -> throw UnsupportedBarcode(type)
    }

    /** Renders a 1D barcode filling [width], with a fixed pixel height. */
    fun barcode(data: String, type: String?, width: Int, height: Int = 120): Bitmap {
        val format = mapType(type)
        val matrix: BitMatrix = MultiFormatWriter().encode(
            data, format, width, height,
            mapOf(EncodeHintType.MARGIN to 4)
        )
        return matrixToBitmap(matrix)
    }

    /** Renders a QR code as a square that fits within [maxWidth]. */
    fun qrCode(data: String, maxWidth: Int): Bitmap {
        val hints = mapOf(
            EncodeHintType.ERROR_CORRECTION to ErrorCorrectionLevel.M,
            EncodeHintType.MARGIN to 2,
        )
        val matrix = MultiFormatWriter().encode(
            data, BarcodeFormat.QR_CODE, maxWidth, maxWidth, hints
        )
        return matrixToBitmap(matrix)
    }

    private fun matrixToBitmap(matrix: BitMatrix): Bitmap {
        val w = matrix.width
        val h = matrix.height
        val pixels = IntArray(w * h)
        for (y in 0 until h) {
            val row = y * w
            for (x in 0 until w) {
                pixels[row + x] = if (matrix.get(x, y)) 0xFF000000.toInt() else 0xFFFFFFFF.toInt()
            }
        }
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        bmp.setPixels(pixels, 0, w, 0, 0, w, h)
        return bmp
    }
}
