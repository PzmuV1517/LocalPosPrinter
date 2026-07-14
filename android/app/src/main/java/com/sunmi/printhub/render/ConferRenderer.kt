package com.sunmi.printhub.render

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint

/**
 * Renders Confer chat messages to the print head, in the layout the product spec calls for:
 *
 *  * **Your** messages are right-aligned (no name — they're yours).
 *  * **Others'** messages are left-aligned: their name, then the message on a `>`-prefixed line.
 *  * **Images** are printed page-wide (full quality, not squeezed to one side); above the image
 *    comes the sender name, a newline, a `>`, a newline, then the image.
 *  * A **separator** rule (labelled with the chat name) is printed when the screen is off and the
 *    source chat changes, so a run of messages is clearly grouped by conversation.
 *
 * Self-contained (own Paints/Canvas) so it doesn't depend on ReceiptRenderer's private helpers.
 */
object ConferRenderer {

    private const val PAD = 14f
    private const val TEXT_SIZE = 30f
    private const val NAME_SIZE = 26f
    private const val SEP_SIZE = 24f

    private fun textPaint(size: Float, bold: Boolean): TextPaint =
        TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.BLACK
            textSize = size
            typeface = if (bold) Typeface.create(Typeface.MONOSPACE, Typeface.BOLD) else Typeface.MONOSPACE
        }

    @Suppress("DEPRECATION")
    private fun layout(text: String, paint: TextPaint, width: Int, align: Layout.Alignment): StaticLayout =
        StaticLayout(text, paint, width.coerceAtLeast(1), align, 1.15f, 0f, false)

    private fun whiteBitmap(w: Int, h: Int): Bitmap =
        Bitmap.createBitmap(w, h.coerceAtLeast(1), Bitmap.Config.ARGB_8888).apply { eraseColor(Color.WHITE) }

    /** Draw a StaticLayout onto a white, full-width bitmap with vertical padding. */
    private fun block(sl: StaticLayout, w: Int, padTop: Float = 4f, padBottom: Float = 8f): Bitmap {
        val h = (sl.height + padTop + padBottom).toInt()
        val bmp = whiteBitmap(w, h)
        val c = Canvas(bmp)
        c.save()
        c.translate(PAD, padTop)
        sl.draw(c)
        c.restore()
        return bmp
    }

    private fun stack(parts: List<Bitmap>, w: Int): Bitmap {
        val total = parts.sumOf { it.height }.coerceAtLeast(1)
        val bmp = whiteBitmap(w, total)
        val c = Canvas(bmp)
        var y = 0f
        for (p in parts) {
            c.drawBitmap(p, 0f, y, null)
            y += p.height
        }
        return bmp
    }

    /** A text message. Your own is right-aligned; others show name + a `>`-prefixed body. */
    fun renderText(name: String, text: String, mine: Boolean, w: Int): Bitmap {
        val inner = (w - 2 * PAD).toInt().coerceAtLeast(1)
        if (mine) {
            return block(layout(text, textPaint(TEXT_SIZE, false), inner, Layout.Alignment.ALIGN_OPPOSITE), w, 6f, 10f)
        }
        val nameBmp = block(layout(name, textPaint(NAME_SIZE, true), inner, Layout.Alignment.ALIGN_NORMAL), w, 6f, 0f)
        val bodyBmp = block(layout("> $text", textPaint(TEXT_SIZE, false), inner, Layout.Alignment.ALIGN_NORMAL), w, 0f, 10f)
        return stack(listOf(nameBmp, bodyBmp), w)
    }

    /** An image message: name / `>` / the image, page-wide (scaled to the full width, dithered). */
    fun renderImage(name: String, image: Bitmap, w: Int): Bitmap {
        val inner = (w - 2 * PAD).toInt().coerceAtLeast(1)
        val nameBmp = block(layout(name, textPaint(NAME_SIZE, true), inner, Layout.Alignment.ALIGN_NORMAL), w, 6f, 0f)
        val gtBmp = block(layout(">", textPaint(TEXT_SIZE, false), inner, Layout.Alignment.ALIGN_NORMAL), w, 0f, 4f)
        val scaled = if (image.width == w) image else ImageUtils.scaleToWidth(image, w)
        val printable = ImageUtils.floydSteinberg(scaled)
        return stack(listOf(nameBmp, gtBmp, printable), w)
    }

    /** A labelled separator rule printed when the active/subscribed chat changes (screen off). */
    fun separator(chatName: String, w: Int): Bitmap {
        val paint = textPaint(SEP_SIZE, true)
        val label = " $chatName "
        val labelW = paint.measureText(label)
        val h = (paint.descent() - paint.ascent() + 20f).toInt()
        val bmp = whiteBitmap(w, h)
        val c = Canvas(bmp)
        val y = h / 2f
        val line = Paint().apply { color = Color.BLACK; strokeWidth = 2f }
        val labelStart = (w - labelW) / 2f
        c.drawLine(PAD, y, labelStart - 6f, y, line)
        c.drawLine(labelStart + labelW + 6f, y, w - PAD, y, line)
        val baseline = y - (paint.descent() + paint.ascent()) / 2f
        c.drawText(label, labelStart, baseline, paint)
        return bmp
    }
}
