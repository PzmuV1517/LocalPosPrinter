package com.sunmi.printhub.render

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import com.sunmi.printhub.model.ImagePosition
import com.sunmi.printhub.model.PrintFormat
import com.sunmi.printhub.model.PrintPayload

/**
 * Renders a [PrintPayload] into a full-width monochrome [Bitmap] ready for the print head.
 *
 * This is the on-device counterpart to the server's Pillow renderer. The two are kept
 * intentionally aligned (same width, margins, borders, dividers, barcode/QR) but the
 * strict pixel-for-pixel guarantee only applies to jobs that go through the server's
 * web-UI preview path (which travel as image_raw_bitmap anyway).
 */
object ReceiptRenderer {

    private const val PAD = 12f
    private const val TEXT_SIZE = 26f
    private const val TITLE_SIZE = 40f
    private const val BORDER = 3f
    private const val DIVIDER = 2f
    private const val TAB_WIDTH = 4
    private const val MIN_TEXT_SIZE = 10
    private const val MAX_TEXT_SIZE = 120

    // MUIE (Minimal Unified Incident Envelope) alert layout.
    // Kept in sync with server/app/render.py (MUIE alert layout).
    private const val ALERT_SIZE = 46f         // the big "ALERT" header
    private const val ALERT_TYPE_SIZE = 24f    // the severity type line
    private const val ALERT_TEXT_SIZE = 32f    // size of the alert message body (most important for legibility)
    private const val ALERT_DASH_SIZE = 15f    // size of the "- - -" dash rule
    private const val ALERT_STAR_SIZE = 15f    // size of the "* * *" star rule
    private const val ALERT_FOOTER_SIZE = 22f
    private const val ALERT_THANKS_SIZE = 18f      // "Thank you for using M.U.I.E."
    private const val ALERT_EXPANSION_SIZE = 15f   // "(Minimal Unified Incident Envelope)"
    private const val ALERT_HEADER_SPACING = 4f    // vertical padding around header lines (ALERT / type)
    private const val ALERT_FOOTER_SPACING = 4f    // vertical padding around footer lines (service/time, thanks, expansion)

    // Font per alert line, chosen by number (see alertFontFiles): 1=Jersey10 (default),
    // 2=built-in mono, 3=Jacquard12, 4=Doto. Missing font files fall back to mono.
    private const val ALERT_FONT = 1
    private const val ALERT_TYPE_FONT = 1
    private const val ALERT_TEXT_FONT = 1
    private const val ALERT_DASH_FONT = 1
    private const val ALERT_STAR_FONT = 1
    private const val ALERT_FOOTER_FONT = 1
    private const val ALERT_THANKS_FONT = 1
    private const val ALERT_EXPANSION_FONT = 1

    class RenderException(message: String) : Exception(message)

    /** Border char sets: tl, horizontal, tr, vertical, bl, br. Plain repeating primitives. */
    private val borders: Map<String, CharArray> = mapOf(
        "dashes" to charArrayOf('+', '-', '+', '|', '+', '+'),
        "equals" to charArrayOf('+', '=', '+', '|', '+', '+'),
        "asterisk" to charArrayOf('*', '*', '*', '*', '*', '*'),
        "at" to charArrayOf('@', '@', '@', '@', '@', '@'),
        "hash" to charArrayOf('#', '#', '#', '#', '#', '#'),
        "dot" to charArrayOf('.', '.', '.', '.', '.', '.'),
        "plus" to charArrayOf('+', '+', '+', '+', '+', '+'),
        "wave" to charArrayOf('+', '~', '+', '|', '+', '+'),
        "box" to charArrayOf('┌', '─', '┐', '│', '└', '┘'),
        "double" to charArrayOf('╔', '═', '╗', '║', '╚', '╝'),
        "rounded" to charArrayOf('╭', '─', '╮', '│', '╰', '╯'),
    )

    // Inline @#@ tags. @#@divider="-=" fills the line; @#@cats inserts a random simple cat.
    private val dividerRegex = Regex("^@#@d[ei]vider\\s*=\\s*\"?(.*?)\"?\\s*$", RegexOption.IGNORE_CASE)

    // Simple, generic ASCII cats written for this project (not copied from any gallery).
    private val cats = listOf(
        " /\\_/\\\n( o.o )\n > ^ <",
        " /\\_/\\\n(=^.^=)\n (\")_(\")",
        " /\\___/\\\n(  o o  )\n(  =^=  )\n (_____)",
        " |\\---/|\n | o_o |\n  \\_^_/",
        "   /\\_/\\\n  ( ^.^ )\n  o(\")(\")",
        " =^..^=",
    )

    fun render(payload: PrintPayload, widthPx: Int): Bitmap {
        val w = widthPx.coerceAtLeast(64)
        val fontNum = payloadFont(payload)
        return when (payload.formatEnum) {
            PrintFormat.IMAGE -> imageOnly(payload, w)
            PrintFormat.BARCODE -> withOptionalTitle(payload, w, fontNum) {
                val code = CodeRenderer.barcode(
                    payload.text ?: throw RenderException("barcode requires 'text'"),
                    payload.barcodeType, (w - 2 * PAD).toInt()
                )
                centerOnWhite(code, w)
            }
            PrintFormat.QRCODE -> withOptionalTitle(payload, w, fontNum) {
                val qr = CodeRenderer.qrCode(
                    payload.text ?: throw RenderException("qrcode requires 'text'"),
                    (w * 0.75f).toInt()
                )
                centerOnWhite(qr, w)
            }
            PrintFormat.ALERT -> alertEnvelope(payload, w)
            else -> textFormatWithOptionalImage(payload, w)
        }
    }

    /** Font number for a whole print, from 'font' (alias legacy 'alert_font'), default 1 (mono). */
    private fun payloadFont(payload: PrintPayload): Int =
        (payload.font ?: payload.alertFont)?.takeIf { it in 1..4 } ?: 1

    // ---- text formats ----

    private fun textFormatWithOptionalImage(payload: PrintPayload, w: Int): Bitmap {
        // Honour a text_size override and expand tabs so whitespace prints as typed.
        val size = clampSize(payload.textSize)
        val fontNum = payloadFont(payload)
        val text = expandTags(expandTabs(payload.text ?: ""), w, size)
        val textBmp = when (payload.formatEnum) {
            PrintFormat.PLAIN -> textBlock(text, size, false, Layout.Alignment.ALIGN_NORMAL, w, PAD, fontNum)
            PrintFormat.CENTERED -> textBlock(text, size, false, Layout.Alignment.ALIGN_CENTER, w, PAD, fontNum)
            PrintFormat.BOXED -> {
                val style = (payload.borderStyle ?: "line").lowercase()
                if (style == "line" || style.isBlank()) boxed(text, w, size, fontNum)
                else asciiBoxed(text, w, style, size, fontNum)
            }
            PrintFormat.HEADER_BODY -> headerBody(payload.title, text, w, size, fontNum)
            PrintFormat.BANNER -> banner(payload.title ?: text, w, fontNum)
            PrintFormat.LIST -> listFormat(payload, w, size, fontNum)
            else -> textBlock(text, size, false, Layout.Alignment.ALIGN_NORMAL, w, PAD, fontNum)
        }

        val imgBmp = decodeImageField(payload, w) ?: return textBmp
        val parts = if (payload.imagePositionEnum == ImagePosition.BOTTOM) {
            listOf(textBmp, imgBmp)
        } else {
            listOf(imgBmp, textBmp)
        }
        return stackVertically(parts, w)
    }

    private fun textBlock(
        text: String, size: Float, bold: Boolean, align: Layout.Alignment, w: Int,
        innerPad: Float = PAD, fontNum: Int = 1,
    ): Bitmap {
        val paint = textPaint(size, bold, fontNum)
        val innerWidth = (w - 2 * innerPad).toInt().coerceAtLeast(1)
        val layout = staticLayout(text, paint, innerWidth, align)
        val h = (layout.height + 2 * innerPad).toInt().coerceAtLeast(1)
        val bmp = whiteBitmap(w, h)
        val c = Canvas(bmp)
        c.save()
        c.translate(innerPad, innerPad)
        layout.draw(c)
        c.restore()
        return bmp
    }

    private fun boxed(text: String, w: Int, size: Float = TEXT_SIZE, fontNum: Int = 1): Bitmap {
        val pad = PAD + BORDER + 6f
        val paint = textPaint(size, false, fontNum)
        val innerWidth = (w - 2 * pad).toInt().coerceAtLeast(1)
        val layout = staticLayout(text, paint, innerWidth, Layout.Alignment.ALIGN_NORMAL)
        val h = (layout.height + 2 * pad).toInt().coerceAtLeast(1)
        val bmp = whiteBitmap(w, h)
        val c = Canvas(bmp)
        c.save()
        c.translate(pad, pad)
        layout.draw(c)
        c.restore()
        // Border rectangle.
        val border = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.BLACK
            style = Paint.Style.STROKE
            strokeWidth = BORDER
        }
        val half = BORDER / 2f
        c.drawRect(half, half, w - half, h - half, border)
        return bmp
    }

    private fun headerBody(title: String?, body: String, w: Int, size: Float = TEXT_SIZE, fontNum: Int = 1): Bitmap {
        val parts = ArrayList<Bitmap>()
        if (!title.isNullOrBlank()) {
            parts.add(textBlock(title, titleSize(size), true, Layout.Alignment.ALIGN_CENTER, w, PAD, fontNum))
            parts.add(dividerBitmap(w))
        }
        parts.add(textBlock(body, size, false, Layout.Alignment.ALIGN_NORMAL, w, PAD, fontNum))
        return stackVertically(parts, w)
    }

    private fun banner(text: String, w: Int, fontNum: Int = 1): Bitmap {
        val innerWidth = (w - 2 * PAD).toInt().coerceAtLeast(1)
        // Grow the font until any wrapped line would exceed the width, then step back.
        var chosen = 30f
        var size = 30f
        while (size <= 160f) {
            val paint = textPaint(size, true, fontNum)
            val layout = staticLayout(text, paint, innerWidth, Layout.Alignment.ALIGN_CENTER)
            var widest = 0f
            for (i in 0 until layout.lineCount) {
                widest = maxOf(widest, layout.getLineWidth(i))
            }
            if (widest > innerWidth) break
            chosen = size
            size += 4f
        }
        return textBlock(text, chosen, true, Layout.Alignment.ALIGN_CENTER, w, PAD, fontNum)
    }

    private fun listFormat(payload: PrintPayload, w: Int, size: Float = TEXT_SIZE, fontNum: Int = 1): Bitmap {
        val parts = ArrayList<Bitmap>()
        if (!payload.title.isNullOrBlank()) {
            parts.add(textBlock(payload.title, titleSize(size), true, Layout.Alignment.ALIGN_CENTER, w, PAD, fontNum))
            parts.add(dividerBitmap(w))
        }
        val items = payload.items ?: emptyList()
        val paint = textPaint(size, false, fontNum)
        for (item in items) {
            parts.add(twoColumnRow(item.label ?: "", item.value ?: "", paint, w))
        }
        if (parts.isEmpty()) parts.add(whiteBitmap(w, 1))
        return stackVertically(parts, w)
    }

    private fun twoColumnRow(label: String, value: String, paint: Paint, w: Int): Bitmap {
        val lineH = (paint.descent() - paint.ascent())
        val h = (lineH + 2 * PAD).toInt().coerceAtLeast(1)
        val bmp = whiteBitmap(w, h)
        val c = Canvas(bmp)
        val baseline = PAD - paint.ascent()
        // value right-aligned; label left-aligned, clipped so it doesn't overrun the value.
        val valueW = paint.measureText(value)
        c.drawText(value, w - PAD - valueW, baseline, paint)
        val labelMax = (w - 2 * PAD - valueW - 10f).coerceAtLeast(10f)
        val labelText = ellipsize(label, paint, labelMax)
        c.drawText(labelText, PAD, baseline, paint)
        return bmp
    }

    // ---- image handling ----

    private fun imageOnly(payload: PrintPayload, w: Int): Bitmap {
        return decodeImageField(payload, w)
            ?: throw RenderException("format 'image' requires 'image' or 'image_raw_bitmap'")
    }

    /** Returns the image for this payload scaled to width, or null if there is none. */
    private fun decodeImageField(payload: PrintPayload, w: Int): Bitmap? {
        if (payload.hasRawBitmap) {
            val raw = ImageUtils.decodeBase64(payload.imageRawBitmap!!)
                ?: throw RenderException("image_raw_bitmap could not be decoded")
            // Printed as-is; only scale if it doesn't already match the width.
            return if (raw.width == w) raw else ImageUtils.scaleToWidth(raw, w)
        }
        if (payload.hasStandardImage) {
            val decoded = ImageUtils.decodeBase64(payload.image!!)
                ?: throw RenderException("image could not be decoded")
            val scaled = ImageUtils.scaleToWidth(decoded, w)
            return ImageUtils.floydSteinberg(scaled)
        }
        return null
    }

    // ---- primitives ----

    private fun withOptionalTitle(payload: PrintPayload, w: Int, fontNum: Int = 1, body: () -> Bitmap): Bitmap {
        val content = body()
        if (payload.title.isNullOrBlank()) return content
        val title = textBlock(payload.title, TITLE_SIZE, true, Layout.Alignment.ALIGN_CENTER, w, PAD, fontNum)
        return stackVertically(listOf(title, dividerBitmap(w), content), w)
    }

    // Alert fonts, selectable by number. Drop TTFs in assets/fonts/ to activate 1/3/4;
    // until then they fall back to the built-in mono font (2).
    private val alertFontFiles = mapOf(
        2 to "Jersey10-Regular.ttf",
        3 to "Jacquard12-Regular.ttf",
        4 to "Doto-Regular.ttf",
    )
    private var assets: android.content.res.AssetManager? = null
    private val typefaceCache = HashMap<Int, Typeface>()

    /** Called once from the Application so custom fonts can be loaded from assets/fonts/. */
    fun loadFonts(context: android.content.Context) {
        assets = context.applicationContext.assets
    }

    private fun typefaceFor(fontNum: Int, bold: Boolean): Typeface {
        // Font 1 (or any number without a bundled file) is the built-in mono font.
        if (!alertFontFiles.containsKey(fontNum)) {
            return if (bold) Typeface.create(Typeface.MONOSPACE, Typeface.BOLD) else Typeface.MONOSPACE
        }
        typefaceCache[fontNum]?.let { return it }
        val am = assets
        val file = alertFontFiles[fontNum]
        val tf = if (am != null && file != null) {
            try {
                Typeface.createFromAsset(am, "fonts/$file")
            } catch (t: Throwable) {
                null
            }
        } else null
        val result = tf ?: Typeface.MONOSPACE // missing file -> mono fallback
        typefaceCache[fontNum] = result
        return result
    }

    private fun textPaint(size: Float, bold: Boolean, fontNum: Int = 1): TextPaint {
        return TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.BLACK
            textSize = size
            typeface = typefaceFor(fontNum, bold)
        }
    }

    @Suppress("DEPRECATION")
    private fun staticLayout(text: String, paint: TextPaint, width: Int, align: Layout.Alignment): StaticLayout {
        return StaticLayout(text, paint, width, align, 1.1f, 0f, false)
    }

    private fun whiteBitmap(w: Int, h: Int): Bitmap {
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        bmp.eraseColor(Color.WHITE)
        return bmp
    }

    private fun dividerBitmap(w: Int): Bitmap {
        val h = (DIVIDER + 2 * PAD).toInt()
        val bmp = whiteBitmap(w, h)
        val c = Canvas(bmp)
        val p = Paint().apply { color = Color.BLACK; strokeWidth = DIVIDER }
        val y = h / 2f
        c.drawLine(PAD, y, w - PAD, y, p)
        return bmp
    }

    private fun centerOnWhite(content: Bitmap, w: Int): Bitmap {
        val h = content.height + (2 * PAD).toInt()
        val bmp = whiteBitmap(w, h)
        val c = Canvas(bmp)
        c.drawBitmap(content, (w - content.width) / 2f, PAD, null)
        return bmp
    }

    // ---- MUIE alert envelope ----

    private fun alertEnvelope(payload: PrintPayload, w: Int): Bitmap {
        val atype = (payload.alertType ?: "alert").trim().uppercase()
        val message = expandTabs(payload.text ?: "")
        val service = (payload.service ?: "unknown").trim()
        val sent = fmtTime(payload.sentAt)
        val recv = fmtTime((System.currentTimeMillis() / 1000).toString()) // the device's own clock
        val footer = "$service\nsent: $sent\nrecv: $recv"

        // A single alert_font on the request overrides every per-line font constant.
        val ov = (payload.font ?: payload.alertFont)?.takeIf { it in 1..4 }
        fun fnt(default: Int): Int = ov ?: default

        val parts = listOf(
            textBlock("ALERT", ALERT_SIZE, true, Layout.Alignment.ALIGN_CENTER, w, ALERT_HEADER_SPACING, fnt(ALERT_FONT)),
            textBlock(atype, ALERT_TYPE_SIZE, true, Layout.Alignment.ALIGN_CENTER, w, ALERT_HEADER_SPACING, fnt(ALERT_TYPE_FONT)),
            dashSpacer(w, fnt(ALERT_DASH_FONT)),
            textBlock(message, ALERT_TEXT_SIZE, false, Layout.Alignment.ALIGN_CENTER, w, PAD, fnt(ALERT_TEXT_FONT)),
            dashSpacer(w, fnt(ALERT_DASH_FONT)),
            starSpacer(w, fnt(ALERT_STAR_FONT)),
            textBlock(footer, ALERT_FOOTER_SIZE, false, Layout.Alignment.ALIGN_CENTER, w, ALERT_FOOTER_SPACING, fnt(ALERT_FOOTER_FONT)),
            starSpacer(w, fnt(ALERT_STAR_FONT)),
            textBlock("Thank you for using M.U.I.E.", ALERT_THANKS_SIZE, false, Layout.Alignment.ALIGN_CENTER, w, ALERT_FOOTER_SPACING, fnt(ALERT_THANKS_FONT)),
            textBlock("(Minimal Unified Incident Envelope)", ALERT_EXPANSION_SIZE, false, Layout.Alignment.ALIGN_CENTER, w, ALERT_FOOTER_SPACING, fnt(ALERT_EXPANSION_FONT)),
        )
        return stackVertically(parts, w)
    }

    private fun dashSpacer(w: Int, fontNum: Int = ALERT_DASH_FONT): Bitmap {
        val paint = textPaint(ALERT_DASH_SIZE, false, fontNum)
        val cw = paint.measureText("M").coerceAtLeast(1f)
        val cols = ((w - 2 * PAD) / cw).toInt().coerceAtLeast(1)
        val s = "- ".repeat(cols / 2 + 1).substring(0, cols).trimEnd()
        return textBlock(s, ALERT_DASH_SIZE, false, Layout.Alignment.ALIGN_CENTER, w, PAD, fontNum)
    }

    private fun starSpacer(w: Int, fontNum: Int = ALERT_STAR_FONT): Bitmap =
        textBlock("* * * * * * *", ALERT_STAR_SIZE, false, Layout.Alignment.ALIGN_CENTER, w, PAD, fontNum)

    /** Format an epoch-seconds value (as string) to a readable time; pass through non-numeric. */
    private fun fmtTime(value: String?): String {
        if (value.isNullOrBlank()) return "—"
        val epoch = value.toDoubleOrNull() ?: return value
        return java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", java.util.Locale.US)
            .format(java.util.Date((epoch * 1000).toLong()))
    }

    private fun stackVertically(parts: List<Bitmap>, w: Int): Bitmap {
        val total = parts.sumOf { it.height }.coerceAtLeast(1)
        val bmp = whiteBitmap(w, total)
        val c = Canvas(bmp)
        var y = 0
        for (p in parts) {
            val x = ((w - p.width) / 2f).coerceAtLeast(0f)
            c.drawBitmap(p, x, y.toFloat(), null)
            y += p.height
        }
        return bmp
    }

    /** Boxed content using a monospace character-grid border (dashes, @, #, box-drawing, …). */
    private fun asciiBoxed(text: String, w: Int, style: String, size: Float = TEXT_SIZE, fontNum: Int = 1): Bitmap {
        val b = borders[style] ?: return boxed(text, w, size, fontNum)
        val paint = textPaint(size, false, fontNum)
        val cw = paint.measureText("M").coerceAtLeast(1f)
        val cols = ((w - 2 * PAD) / cw).toInt()
        if (cols < 5) return boxed(text, w, size, fontNum)
        val inner = cols - 2
        val tl = b[0]; val hz = b[1]; val tr = b[2]; val vt = b[3]; val bl = b[4]; val br = b[5]

        val lines = ArrayList<String>()
        lines.add("$tl${hz.toString().repeat(inner)}$tr")
        for (para in text.split("\n")) {
            for (wl in wrapChars(para, inner)) {
                lines.add("$vt${wl.padEnd(inner)}$vt")
            }
        }
        lines.add("$bl${hz.toString().repeat(inner)}$br")
        return monoBlock(lines, w, paint)
    }

    /** Draws pre-formatted monospace lines verbatim — no wrapping, no space collapsing. */
    private fun monoBlock(lines: List<String>, w: Int, paint: TextPaint): Bitmap {
        val lh = paint.descent() - paint.ascent()
        val h = (lh * lines.size + 2 * PAD).toInt().coerceAtLeast(1)
        val bmp = whiteBitmap(w, h)
        val c = Canvas(bmp)
        var baseline = PAD - paint.ascent()
        for (line in lines) {
            c.drawText(line, PAD, baseline, paint)
            baseline += lh
        }
        return bmp
    }

    /** Wrap to a fixed column count preserving all whitespace (leading/multiple/trailing). */
    private fun wrapChars(text: String, maxChars: Int): List<String> {
        val out = ArrayList<String>()
        for (para in text.split("\n")) {
            if (para.isEmpty()) {
                out.add("")
                continue
            }
            var start = 0
            val n = para.length
            while (n - start > maxChars) {
                val window = para.substring(start, start + maxChars)
                val brk = window.lastIndexOf(' ')
                if (brk <= 0) {
                    out.add(para.substring(start, start + maxChars))
                    start += maxChars
                } else {
                    out.add(para.substring(start, start + brk))
                    start += brk + 1 // consume the single break space
                }
            }
            out.add(para.substring(start))
        }
        return out
    }

    /** Title size scaled proportionally to the body size. */
    private fun titleSize(size: Float): Float =
        maxOf(size + 4f, Math.round(size * TITLE_SIZE / TEXT_SIZE).toFloat())

    /** Effective body text size from the payload, clamped to a sane range. */
    private fun clampSize(value: Int?): Float =
        (value ?: TEXT_SIZE.toInt()).coerceIn(MIN_TEXT_SIZE, MAX_TEXT_SIZE).toFloat()

    /** Expand tabs to spaces (per-line tab stops) so tabs print as alignment, not blanks. */
    private fun expandTabs(text: String, tabWidth: Int = TAB_WIDTH): String {
        if (text.indexOf('\t') < 0) return text
        val sb = StringBuilder()
        for (line in text.split("\n")) {
            if (sb.isNotEmpty()) sb.append('\n')
            var col = 0
            for (ch in line) {
                if (ch == '\t') {
                    val spaces = tabWidth - (col % tabWidth)
                    repeat(spaces) { sb.append(' ') }
                    col += spaces
                } else {
                    sb.append(ch)
                    col++
                }
            }
        }
        return sb.toString()
    }

    /** Expand inline @#@ tags line-by-line. Dividers fill the print width in monospace cells. */
    private fun expandTags(text: String, w: Int, size: Float = TEXT_SIZE): String {
        if (!text.contains("@#@")) return text
        val paint = textPaint(size, false)
        val cw = paint.measureText("M").coerceAtLeast(1f)
        val cols = ((w - 2 * PAD) / cw).toInt().coerceAtLeast(1)
        val lines = text.split("\n")
        val out = ArrayList<String>()
        for (line in lines) {
            val stripped = line.trim()
            val m = dividerRegex.find(stripped)
            when {
                m != null -> out.add(fillPattern(m.groupValues[1], cols))
                stripped.lowercase().startsWith("@#@cats") -> out.addAll(cats.random().split("\n"))
                else -> out.add(line)
            }
        }
        return out.joinToString("\n")
    }

    private fun fillPattern(pattern: String, cols: Int): String {
        val p = if (pattern.isEmpty()) "-" else pattern
        val sb = StringBuilder()
        while (sb.length < cols) sb.append(p)
        return sb.substring(0, cols)
    }

    private fun ellipsize(text: String, paint: Paint, maxWidth: Float): String {
        if (paint.measureText(text) <= maxWidth) return text
        val ell = "…"
        var end = text.length
        while (end > 0 && paint.measureText(text.substring(0, end) + ell) > maxWidth) end--
        return text.substring(0, end) + ell
    }
}
