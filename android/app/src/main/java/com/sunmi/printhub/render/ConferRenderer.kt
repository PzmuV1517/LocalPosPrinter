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
 *  * **Your** messages are right-aligned (no name, they're yours).
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

    /**
     * A text message. Your own sits on the right, your name, then the message closed with a `<`.
     * Others sit on the left, their name, then the message opened with a `>`. The arrows point
     * inward toward each speaker's side. [showName] is false for a run of messages from the same
     * sender, so consecutive messages group tightly without repeating the name.
     */
    fun renderText(name: String, text: String, mine: Boolean, showName: Boolean, w: Int): Bitmap {
        val inner = (w - 2 * PAD).toInt().coerceAtLeast(1)
        val align = if (mine) Layout.Alignment.ALIGN_OPPOSITE else Layout.Alignment.ALIGN_NORMAL
        val body = if (mine) "$text <" else "> $text"
        val parts = ArrayList<Bitmap>()
        // A little extra top gap before a new speaker; grouped messages stay snug.
        if (showName) parts.add(block(layout(name, textPaint(NAME_SIZE, true), inner, align), w, 10f, 0f))
        parts.add(block(layout(body, textPaint(TEXT_SIZE, false), inner, align), w, if (showName) 0f else 2f, 4f))
        return if (parts.size == 1) parts[0] else stack(parts, w)
    }

    /** An image message: name / `>` / the image, page-wide (scaled to the full width, dithered). */
    fun renderImage(name: String, image: Bitmap, showName: Boolean, w: Int): Bitmap {
        val inner = (w - 2 * PAD).toInt().coerceAtLeast(1)
        val scaled = if (image.width == w) image else ImageUtils.scaleToWidth(image, w)
        val printable = ImageUtils.floydSteinberg(scaled)
        val parts = ArrayList<Bitmap>()
        if (showName) parts.add(block(layout(name, textPaint(NAME_SIZE, true), inner, Layout.Alignment.ALIGN_NORMAL), w, 10f, 0f))
        parts.add(block(layout(">", textPaint(TEXT_SIZE, false), inner, Layout.Alignment.ALIGN_NORMAL), w, 0f, 4f))
        parts.add(printable)
        return stack(parts, w)
    }

    /** A terminal-style banner printed when a chat is opened, to head its paper transcript. */
    fun transcriptStart(chatName: String, w: Int): Bitmap {
        val inner = (w - 2 * PAD).toInt().coerceAtLeast(1)
        val rule = block(layout(slashRule(inner), textPaint(20f, true), inner, Layout.Alignment.ALIGN_CENTER), w, 12f, 0f)
        val head = block(layout("//// TRANSCRIPT START ////", textPaint(28f, true), inner, Layout.Alignment.ALIGN_CENTER), w, 2f, 0f)
        val name = block(layout(chatName, textPaint(36f, true), inner, Layout.Alignment.ALIGN_CENTER), w, 2f, 2f)
        val rule2 = block(layout(slashRule(inner), textPaint(20f, true), inner, Layout.Alignment.ALIGN_CENTER), w, 0f, 12f)
        return stack(listOf(rule, head, name, rule2), w)
    }

    /** Terminal-style "boot sequence" printed when entering Confer mode. [lines] are diagnostics. */
    fun conferStartup(lines: List<String>, w: Int): Bitmap {
        val inner = (w - 2 * PAD).toInt().coerceAtLeast(1)
        val parts = ArrayList<Bitmap>()
        parts.add(block(layout(slashRule(inner), textPaint(20f, true), inner, Layout.Alignment.ALIGN_CENTER), w, 14f, 0f))
        parts.add(block(layout("C O N F E R", textPaint(38f, true), inner, Layout.Alignment.ALIGN_CENTER), w, 2f, 0f))
        parts.add(block(layout("secure transcript link", textPaint(20f, false), inner, Layout.Alignment.ALIGN_CENTER), w, 0f, 2f))
        parts.add(block(layout(slashRule(inner), textPaint(20f, true), inner, Layout.Alignment.ALIGN_CENTER), w, 0f, 6f))
        for (l in lines) {
            parts.add(block(layout(l, textPaint(24f, false), inner, Layout.Alignment.ALIGN_NORMAL), w, 0f, 2f))
        }
        parts.add(block(layout(slashRule(inner), textPaint(20f, true), inner, Layout.Alignment.ALIGN_CENTER), w, 6f, 6f))
        return stack(parts, w)
    }

    private fun slashRule(innerPx: Int): String {
        val paint = textPaint(20f, true)
        val cw = paint.measureText("/").coerceAtLeast(1f)
        val cols = (innerPx / cw).toInt().coerceIn(8, 64)
        return "/".repeat(cols)
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
