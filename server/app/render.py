"""
Shared Pillow renderer for the companion server.

This is the *single* rendering implementation for anything going through the web UI:
both ``/preview`` and ``/print`` call :func:`render`, so whatever the preview shows is
pixel-for-pixel what gets shipped to the printer (as ``image_raw_bitmap``).

It mirrors the Android ``ReceiptRenderer`` format-by-format (same width, margins, border,
divider, barcode/QR logic). The strict 1:1 guarantee is between preview and print on this
server; it is not claimed against the app's own on-device Canvas renderer.
"""

from __future__ import annotations

import base64
import io
import math
import os
import random
import re
import time
from datetime import datetime
from typing import List, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

# ---- layout constants (kept aligned with the Android renderer) ----
DEFAULT_WIDTH = int(os.environ.get("PRINT_WIDTH", "384"))
PAD = 12
TEXT_SIZE = 30
TITLE_SIZE = 44
BORDER = 3
DIVIDER = 2
TAB_WIDTH = 4
MIN_TEXT_SIZE = 10
MAX_TEXT_SIZE = 120

# MUIE (Minimal Unified Incident Envelope) alert layout.
ALERT_SIZE = 62            # the big "ALERT" header
ALERT_TYPE_SIZE = 34       # the severity type line
ALERT_TEXT_SIZE = 44       # size of the alert message body (most important for legibility)
ALERT_DASH_SIZE = 20       # size of the "- - -" dash rule
ALERT_STAR_SIZE = 20       # size of the "* * *" star rule
ALERT_FOOTER_SIZE = 30
ALERT_THANKS_SIZE = 24     # "Thank you for using M.U.I.E."
ALERT_EXPANSION_SIZE = 20  # "(Minimal Unified Incident Envelope)"
ALERT_HEADER_SPACING = 6   # vertical padding around header lines (ALERT / type)
ALERT_FOOTER_SPACING = 6   # vertical padding around footer lines (service/time, thanks, expansion)
# Font per alert line, chosen by number (see _ALERT_FONT_FILES): 1=Jersey10 (default),
# 2=built-in mono, 3=Jacquard12, 4=Doto. Missing font files fall back to the mono font.
ALERT_FONT = 1
ALERT_TYPE_FONT = 1
ALERT_TEXT_FONT = 1
ALERT_DASH_FONT = 1
ALERT_STAR_FONT = 1
ALERT_FOOTER_FONT = 1
ALERT_THANKS_FONT = 1
ALERT_EXPANSION_FONT = 1
ALERT_SEVERITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]

WHITE = 255
BLACK = 0


def _clamp_size(value) -> int:
    """Effective body text size from the payload's text_size, clamped to a sane range."""
    try:
        if value is None or str(value).strip() == "":
            return TEXT_SIZE
        return max(MIN_TEXT_SIZE, min(MAX_TEXT_SIZE, int(value)))
    except (TypeError, ValueError):
        return TEXT_SIZE


class RenderError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Fonts, one explicit bundled TTF for determinism regardless of host OS.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(__file__)
_FONT_CANDIDATES = [
    os.environ.get("FONT_PATH"),
    os.path.join(_HERE, "fonts", "DejaVuSansMono.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/Library/Fonts/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]
_BOLD_CANDIDATES = [
    os.environ.get("FONT_PATH_BOLD"),
    os.path.join(_HERE, "fonts", "DejaVuSansMono-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]


def _first_existing(paths) -> Optional[str]:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


_REGULAR_PATH = _first_existing(_FONT_CANDIDATES)
_BOLD_PATH = _first_existing(_BOLD_CANDIDATES) or _REGULAR_PATH
_font_cache: dict = {}

# Alert fonts, selectable by number. Drop the TTFs in app/fonts/ to activate 1/3/4;
# until then they gracefully fall back to the built-in mono font (2).
_ALERT_FONT_FILES = {
    1: None,                        # built-in mono (DejaVuSansMono / Menlo), default
    2: "Jersey10-Regular.ttf",      # pixel font
    3: "Jacquard12-Regular.ttf",    # pixel font
    4: "Doto-Regular.ttf",          # dot-matrix font
}


def _font_path_for(font_num: int, bold: bool) -> Optional[str]:
    fname = _ALERT_FONT_FILES.get(font_num)
    if fname:
        p = os.path.join(_HERE, "fonts", fname)
        if os.path.isfile(p):
            return p
    # font 2, unknown number, or a missing file -> the built-in mono font.
    return _BOLD_PATH if bold else _REGULAR_PATH


def _font(size: int, bold: bool, font_num: int = 1) -> ImageFont.FreeTypeFont:
    # Resolve the path first and cache on it, so a font file dropped in while the server is
    # running is picked up on the next render (a stale mono-fallback resolves to a new path).
    path = _font_path_for(font_num, bold)
    key = (path, size)
    if key in _font_cache:
        return _font_cache[key]
    if path:
        font = ImageFont.truetype(path, size)
    else:
        # Last resort: non-scalable bitmap font. Drop a TTF in app/fonts/ for real output.
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def _blank(w: int, h: int) -> Image.Image:
    return Image.new("1", (w, max(1, h)), WHITE)


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    return draw.textlength(text, font=font)


def _cols_for(font, max_width: int) -> int:
    """How many monospace cells fit in max_width."""
    scratch = ImageDraw.Draw(_blank(1, 1))
    cw = _text_width(scratch, "M", font) or 1
    return max(1, int(max_width // cw))


def _wrap_cols(text: str, cols: int) -> List[str]:
    """Wrap to a fixed column count while preserving ALL whitespace (leading, multiple,
    trailing). Breaks at the last space in the window, else hard-breaks a long run."""
    lines: List[str] = []
    for para in text.split("\n"):
        if para == "":
            lines.append("")
            continue
        start = 0
        n = len(para)
        while n - start > cols:
            window = para[start:start + cols]
            brk = window.rfind(" ")
            if brk <= 0:
                lines.append(para[start:start + cols])
                start += cols
            else:
                lines.append(para[start:start + brk])
                start += brk + 1  # consume the single break space
        lines.append(para[start:])
    return lines


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
    return _wrap_cols(text or "", _cols_for(font, max_width))


def _text_block(text: str, size: int, bold: bool, align: str, w: int, pad: int = PAD,
                font_num: int = 1) -> Image.Image:
    font = _font(size, bold, font_num)
    inner = max(1, w - 2 * pad)
    scratch = ImageDraw.Draw(_blank(1, 1))
    lines = _wrap(scratch, text or "", font, inner)
    lh = _line_height(font)
    h = lh * max(1, len(lines)) + 2 * pad
    img = _blank(w, h)
    draw = ImageDraw.Draw(img)
    y = pad
    for line in lines:
        lw = _text_width(draw, line, font)
        if align == "center":
            x = (w - lw) / 2
        elif align == "right":
            x = w - pad - lw
        else:
            x = pad
        draw.text((x, y), line, font=font, fill=BLACK)
        y += lh
    return img


def _divider(w: int) -> Image.Image:
    h = DIVIDER + 2 * PAD
    img = _blank(w, h)
    draw = ImageDraw.Draw(img)
    y = h // 2
    draw.line([(PAD, y), (w - PAD, y)], fill=BLACK, width=DIVIDER)
    return img


def _stack(parts: List[Image.Image], w: int) -> Image.Image:
    total = sum(p.height for p in parts) or 1
    img = _blank(w, total)
    y = 0
    for p in parts:
        x = max(0, (w - p.width) // 2)
        img.paste(p, (x, y))
        y += p.height
    return img


def _center_on_white(content: Image.Image, w: int) -> Image.Image:
    h = content.height + 2 * PAD
    img = _blank(w, h)
    img.paste(content, (max(0, (w - content.width) // 2), PAD))
    return img


# ---------------------------------------------------------------------------
# Format renderers
# ---------------------------------------------------------------------------
# Border character sets: (top-left, horizontal, top-right, vertical, bottom-left, bottom-right).
# Plain repeating-character primitives, nothing copied from any art collection.
BORDERS = {
    "dashes":   ("+", "-", "+", "|", "+", "+"),
    "equals":   ("+", "=", "+", "|", "+", "+"),
    "asterisk": ("*", "*", "*", "*", "*", "*"),
    "at":       ("@", "@", "@", "@", "@", "@"),
    "hash":     ("#", "#", "#", "#", "#", "#"),
    "dot":      (".", ".", ".", ".", ".", "."),
    "plus":     ("+", "+", "+", "+", "+", "+"),
    "wave":     ("+", "~", "+", "|", "+", "+"),
    "box":      ("┌", "─", "┐", "│", "└", "┘"),
    "double":   ("╔", "═", "╗", "║", "╚", "╝"),
    "rounded":  ("╭", "─", "╮", "│", "╰", "╯"),
}


def _wrap_chars(text: str, max_chars: int) -> List[str]:
    """Wrap at a fixed character count preserving whitespace (monospace grid)."""
    return _wrap_cols(text, max_chars) or [""]


def _title_size(size: int) -> int:
    """Title size scaled proportionally to the body size."""
    return max(size + 4, round(size * TITLE_SIZE / TEXT_SIZE))


def _mono_block(lines: List[str], w: int, size: int = TEXT_SIZE, font_num: int = 1) -> Image.Image:
    """Draw pre-formatted monospace lines verbatim (no re-wrapping / space collapsing)."""
    font = _font(size, False, font_num)
    lh = _line_height(font)
    img = _blank(w, lh * max(1, len(lines)) + 2 * PAD)
    draw = ImageDraw.Draw(img)
    y = PAD
    for line in lines:
        draw.text((PAD, y), line, font=font, fill=BLACK)
        y += lh
    return img


def _ascii_boxed(text: str, w: int, style: str, size: int = TEXT_SIZE, font_num: int = 1) -> Image.Image:
    border = BORDERS.get(style)
    if border is None:
        return _boxed(text, w, size, font_num)  # unknown style -> fall back to the drawn rectangle
    font = _font(size, False, font_num)
    scratch = ImageDraw.Draw(_blank(1, 1))
    cw = _text_width(scratch, "M", font) or 1
    cols = int((w - 2 * PAD) // cw)
    if cols < 5:
        return _boxed(text, w, size, font_num)
    inner = cols - 2
    tl, hz, tr, vt, bl, br = border
    lines = [tl + hz * inner + tr]
    for para in (text or "").split("\n"):
        for wl in _wrap_chars(para, inner):
            lines.append(vt + wl.ljust(inner) + vt)
    lines.append(bl + hz * inner + br)
    return _mono_block(lines, w, size, font_num)


def _boxed(text: str, w: int, size: int = TEXT_SIZE, font_num: int = 1) -> Image.Image:
    pad = PAD + BORDER + 6
    inner = _text_block(text, size, False, "left", w, pad=pad, font_num=font_num)
    draw = ImageDraw.Draw(inner)
    half = BORDER // 2
    draw.rectangle(
        [half, half, w - 1 - half, inner.height - 1 - half],
        outline=BLACK, width=BORDER,
    )
    return inner


def _header_body(title: Optional[str], body: str, w: int, size: int = TEXT_SIZE, font_num: int = 1) -> Image.Image:
    parts = []
    if title:
        parts.append(_text_block(title, _title_size(size), True, "center", w, font_num=font_num))
        parts.append(_divider(w))
    parts.append(_text_block(body, size, False, "left", w, font_num=font_num))
    return _stack(parts, w)


def _banner(text: str, w: int, font_num: int = 1) -> Image.Image:
    inner = max(1, w - 2 * PAD)
    scratch = ImageDraw.Draw(_blank(1, 1))
    # Grow the font until the widest *whole word* no longer fits on a line. Using the
    # widest word (not char-broken lines) avoids runaway scaling on long strings.
    words = text.split() or [text]
    chosen = 30
    size = 30
    while size <= 160:
        font = _font(size, True, font_num)
        widest_word = max(_text_width(scratch, wd, font) for wd in words)
        if widest_word > inner:
            break
        chosen = size
        size += 4
    return _text_block(text, chosen, True, "center", w, font_num=font_num)


def _list_format(payload: dict, w: int, size: int = TEXT_SIZE, font_num: int = 1) -> Image.Image:
    parts = []
    title = payload.get("title")
    if title:
        parts.append(_text_block(title, _title_size(size), True, "center", w, font_num=font_num))
        parts.append(_divider(w))
    font = _font(size, False, font_num)
    lh = _line_height(font)
    for item in payload.get("items") or []:
        parts.append(_two_column(str(item.get("label", "")), str(item.get("value", "")), font, lh, w))
    if not parts:
        parts.append(_blank(w, 1))
    return _stack(parts, w)


def _two_column(label: str, value: str, font, lh: int, w: int) -> Image.Image:
    h = lh + 2 * PAD
    img = _blank(w, h)
    draw = ImageDraw.Draw(img)
    vw = _text_width(draw, value, font)
    draw.text((w - PAD - vw, PAD), value, font=font, fill=BLACK)
    label_max = max(10, w - 2 * PAD - vw - 10)
    label = _ellipsize(draw, label, font, label_max)
    draw.text((PAD, PAD), label, font=font, fill=BLACK)
    return img


def _ellipsize(draw, text, font, max_width) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    ell = "…"
    end = len(text)
    while end > 0 and _text_width(draw, text[:end] + ell, font) > max_width:
        end -= 1
    return text[:end] + ell


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def _decode_b64(data: str) -> bytes:
    if "base64," in data:
        data = data.split("base64,", 1)[1]
    return base64.b64decode(data)


def _scale_to_width(img: Image.Image, w: int) -> Image.Image:
    if img.width == w:
        return img
    ratio = w / img.width
    h = max(1, round(img.height * ratio))
    return img.resize((w, h), Image.LANCZOS)


def _image_field(payload: dict, w: int) -> Optional[Image.Image]:
    raw = payload.get("image_raw_bitmap")
    if raw:
        try:
            img = Image.open(io.BytesIO(_decode_b64(raw))).convert("1")  # printed as-is, no re-dither
        except Exception as exc:
            raise RenderError(f"Could not decode image_raw_bitmap: {exc}") from exc
        return _scale_to_width(img, w) if img.width != w else img
    std = payload.get("image")
    if std:
        try:
            img = Image.open(io.BytesIO(_decode_b64(std))).convert("L")
        except Exception as exc:
            raise RenderError(f"Could not decode image (use PNG/JPEG): {exc}") from exc
        img = _adjust_image(img, payload)
        img = _scale_to_width(img, w)
        return _to_1bit(img, payload)
    return None


def _fnum(payload: dict, key: str, default: float) -> float:
    try:
        return float(payload.get(key, default))
    except (TypeError, ValueError):
        return default


def _adjust_image(img: Image.Image, payload: dict) -> Image.Image:
    """Grayscale tweaks so faint photos print with visible detail. img is mode 'L'."""
    if payload.get("image_autocontrast"):
        img = ImageOps.autocontrast(img, cutoff=1)
    b = _fnum(payload, "image_brightness", 1.0)
    if b != 1.0:
        img = ImageEnhance.Brightness(img).enhance(b)
    c = _fnum(payload, "image_contrast", 1.0)
    if c != 1.0:
        img = ImageEnhance.Contrast(img).enhance(c)
    if payload.get("image_sharpen"):
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=2))
    if payload.get("image_invert"):
        img = ImageOps.invert(img)
    return img


def _to_1bit(img: Image.Image, payload: dict) -> Image.Image:
    """Convert 'L' -> 1-bit. 'threshold' = hard cutoff (logos/line art), else Floyd–Steinberg."""
    mode = (payload.get("image_dither") or "fs").lower()
    if mode in ("threshold", "none"):
        try:
            t = int(payload.get("image_threshold", 128))
        except (TypeError, ValueError):
            t = 128
        return img.point(lambda p: 255 if p >= t else 0).convert("1")
    return img.convert("1")  # Floyd–Steinberg (Pillow default dither), best for photos


# ---------------------------------------------------------------------------
# Barcode / QR
# ---------------------------------------------------------------------------
_BARCODE_MAP = {
    "CODE128": "code128", "CODE_128": "code128",
    "CODE39": "code39", "CODE_39": "code39",
    "EAN13": "ean13", "EAN_13": "ean13",
    "EAN8": "ean8", "EAN_8": "ean8",
    "UPC_A": "upca", "UPCA": "upca",
    "ITF": "itf",
    "CODABAR": "codabar",
}


def _barcode(data: str, btype: Optional[str], w: int) -> Image.Image:
    import barcode
    from barcode.writer import ImageWriter

    name = _BARCODE_MAP.get((btype or "").upper())
    if not name:
        raise RenderError(f"Unsupported or missing barcode_type: {btype}")
    writer = ImageWriter()
    try:
        obj = barcode.get(name, data, writer=writer)
    except Exception as exc:  # noqa: BLE001 - surface a clear message to the caller
        raise RenderError(f"Could not encode barcode: {exc}") from exc
    img = obj.render(writer_options={"module_height": 12.0, "quiet_zone": 2.0, "font_size": 8})
    img = img.convert("L")
    img = _scale_to_width(img, int(w - 2 * PAD))
    return _center_on_white(img.convert("1"), w)


def _qrcode(data: str, w: int) -> Image.Image:
    import qrcode

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("1")
    target = int(w * 0.75)
    img = img.resize((target, target), Image.NEAREST)
    return _center_on_white(img, w)


# ---------------------------------------------------------------------------
# MUIE, Minimal Unified Incident Envelope (alert format)
# ---------------------------------------------------------------------------
def _fmt_time(value) -> str:
    """Format a timestamp. Accepts epoch seconds (number/str) or a preformatted string."""
    if value is None or value == "":
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError):
        return str(value)


def _dash_spacer(w: int, font_num: int = ALERT_DASH_FONT) -> Image.Image:
    """Full-width '- - - - -' rule in a small font."""
    cols = _cols_for(_font(ALERT_DASH_SIZE, False, font_num), w - 2 * PAD)
    s = ("- " * (cols // 2 + 1))[:cols].rstrip()
    return _text_block(s, ALERT_DASH_SIZE, False, "center", w, font_num=font_num)


def _star_spacer(w: int, font_num: int = ALERT_STAR_FONT) -> Image.Image:
    """Short, centered '* * *' rule (deliberately not the full page width)."""
    return _text_block("* * * * * * *", ALERT_STAR_SIZE, False, "center", w, font_num=font_num)


def _payload_font(payload: dict, default: int = 1) -> int:
    """Font number for a whole print, from the 'font' field (alias 'alert_font'). Any
    format can pick a font this way; falls back to [default] when absent/invalid."""
    for key in ("font", "alert_font"):
        try:
            n = int(payload.get(key))
            if n in _ALERT_FONT_FILES:
                return n
        except (TypeError, ValueError):
            continue
    return default


def _alert_font_override(payload: dict) -> Optional[int]:
    """A 'font'/'alert_font' number from the request overrides every per-line font constant."""
    for key in ("font", "alert_font"):
        try:
            n = int(payload.get(key))
            if n in _ALERT_FONT_FILES:
                return n
        except (TypeError, ValueError):
            continue
    return None


def _alert(payload: dict, w: int) -> Image.Image:
    """The MUIE envelope: ALERT / type / dash / message / dash / stars / footer / stars."""
    atype = (payload.get("alert_type") or payload.get("type") or "alert").strip().upper()
    message = (payload.get("text") or payload.get("message") or "").expandtabs(TAB_WIDTH)
    service = (payload.get("service") or "unknown").strip()
    sent = _fmt_time(payload.get("sent_at"))
    recv = _fmt_time(time.time())  # the receiving app's own clock

    ov = _alert_font_override(payload)

    def fnt(default_const: int) -> int:
        return ov if ov else default_const

    footer = f"{service}\nsent: {sent}\nrecv: {recv}"
    parts = [
        _text_block("ALERT", ALERT_SIZE, True, "center", w, pad=ALERT_HEADER_SPACING, font_num=fnt(ALERT_FONT)),
        _text_block(atype, ALERT_TYPE_SIZE, True, "center", w, pad=ALERT_HEADER_SPACING, font_num=fnt(ALERT_TYPE_FONT)),
        _dash_spacer(w, fnt(ALERT_DASH_FONT)),
        _text_block(message, ALERT_TEXT_SIZE, False, "center", w, font_num=fnt(ALERT_TEXT_FONT)),
        _dash_spacer(w, fnt(ALERT_DASH_FONT)),
        _star_spacer(w, fnt(ALERT_STAR_FONT)),
        _text_block(footer, ALERT_FOOTER_SIZE, False, "center", w, pad=ALERT_FOOTER_SPACING, font_num=fnt(ALERT_FOOTER_FONT)),
        _star_spacer(w, fnt(ALERT_STAR_FONT)),
        _text_block("Thank you for using M.U.I.E.", ALERT_THANKS_SIZE, False, "center", w, pad=ALERT_FOOTER_SPACING, font_num=fnt(ALERT_THANKS_FONT)),
        _text_block("(Minimal Unified Incident Envelope)", ALERT_EXPANSION_SIZE, False, "center", w, pad=ALERT_FOOTER_SPACING, font_num=fnt(ALERT_EXPANSION_FONT)),
    ]
    return _stack(parts, w)


# ---------------------------------------------------------------------------
# Inline @#@ tags (processed inside text before layout)
# ---------------------------------------------------------------------------
# @#@divider="-="  -> repeat the pattern to fill the line width
# @#@cats          -> insert a random simple cat
_DIVIDER_RE = re.compile(r'^@#@d[ei]vider\s*=\s*"?(.*?)"?\s*$', re.IGNORECASE)

# Simple, generic ASCII cats written for this project (not copied from any gallery).
CATS: List[str] = [
    " /\\_/\\\n( o.o )\n > ^ <",
    " /\\_/\\\n(=^.^=)\n (\")_(\")",
    " /\\___/\\\n(  o o  )\n(  =^=  )\n (_____)",
    " |\\---/|\n | o_o |\n  \\_^_/",
    "   /\\_/\\\n  ( ^.^ )\n  o(\")(\")",
    " =^..^=",
]


def _fill_pattern(pattern: str, cols: int) -> str:
    if not pattern:
        pattern = "-"
    reps = cols // len(pattern) + 1
    return (pattern * reps)[:cols]


def expand_tags(text: str, w: int, size: int = TEXT_SIZE) -> str:
    """Expand inline @#@ tags line-by-line. Dividers fill the print width in monospace cells."""
    if not text or "@#@" not in text:
        return text
    cols = _cols_for(_font(size, False), w - 2 * PAD)

    out: List[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        m = _DIVIDER_RE.match(stripped)
        if m is not None:
            out.append(_fill_pattern(m.group(1), cols))
            continue
        if stripped.lower().startswith("@#@cats"):
            out.extend(random.choice(CATS).split("\n"))
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def render(payload: dict, width: int = DEFAULT_WIDTH) -> Image.Image:
    """Render a payload dict to a 1-bit :class:`PIL.Image` at the given width."""
    w = max(64, int(width))
    fmt = (payload.get("format") or "plain").lower()

    if fmt == "alert":
        return _alert(payload, w)

    if fmt == "image":
        img = _image_field(payload, w)
        if img is None:
            raise RenderError("format 'image' requires 'image' or 'image_raw_bitmap'")
        return img

    # A single 'font' number (alias 'alert_font') selects the font for any format.
    font_num = _payload_font(payload)

    if fmt == "barcode":
        text = payload.get("text")
        if not text:
            raise RenderError("barcode requires 'text'")
        content = _barcode(text, payload.get("barcode_type"), w)
        return _with_optional_title(payload, w, content, font_num)

    if fmt == "qrcode":
        text = payload.get("text")
        if not text:
            raise RenderError("qrcode requires 'text'")
        content = _qrcode(text, w)
        return _with_optional_title(payload, w, content, font_num)

    # text formats: honour a text_size override and expand tabs so whitespace prints as typed.
    size = _clamp_size(payload.get("text_size"))
    raw = (payload.get("text") or "").expandtabs(TAB_WIDTH)
    text = expand_tags(raw, w, size)
    if fmt == "centered":
        text_img = _text_block(text, size, False, "center", w, font_num=font_num)
    elif fmt == "boxed":
        style = (payload.get("border_style") or "line").lower()
        text_img = (_boxed(text, w, size, font_num) if style in ("line", "")
                    else _ascii_boxed(text, w, style, size, font_num))
    elif fmt == "header_body":
        text_img = _header_body(payload.get("title"), text, w, size, font_num)
    elif fmt == "banner":
        text_img = _banner(payload.get("title") or text, w, font_num)  # banner size is auto-scaled
    elif fmt == "list":
        text_img = _list_format(payload, w, size, font_num)
    else:  # plain / unknown
        text_img = _text_block(text, size, False, "left", w, font_num=font_num)

    img = _image_field(payload, w)
    if img is None:
        return text_img
    position = (payload.get("image_position") or "top").lower()
    parts = [text_img, img] if position == "bottom" else [img, text_img]
    return _stack(parts, w)


def _with_optional_title(payload: dict, w: int, content: Image.Image, font_num: int = 1) -> Image.Image:
    title = payload.get("title")
    if not title:
        return content
    return _stack([_text_block(title, TITLE_SIZE, True, "center", w, font_num=font_num), _divider(w), content], w)


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def to_base64_png(img: Image.Image) -> str:
    return base64.b64encode(to_png_bytes(img)).decode("ascii")


# ---------------------------------------------------------------------------
# Daily brief (weather + systems), a terminal-style morning readout with graphics.
# ---------------------------------------------------------------------------
_WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
    85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm", 96: "Thunderstorm, hail",
    99: "Thunderstorm, hail",
}


def _vstack(imgs: List[Image.Image], w: int) -> Image.Image:
    imgs = [i for i in imgs if i.height > 0]
    out = _blank(w, sum(i.height for i in imgs) or 1)
    y = 0
    for i in imgs:
        out.paste(i, ((w - i.width) // 2, y))
        y += i.height
    return out


def _rule_img(w: int) -> Image.Image:
    return _text_block("/" * 24, 20, True, "center", w)


def _sun_arc(w: int, sunrise: str, sunset: str) -> Image.Image:
    h = 112
    img = _blank(w, h)
    d = ImageDraw.Draw(img)
    left, right = 44, w - 44
    base = h - 24
    ry = 40
    d.arc([left, base - 2 * ry, right, base], 180, 360, fill=BLACK, width=2)
    d.line([left - 8, base, right + 8, base], fill=BLACK, width=1)
    frac = 0.5
    try:
        sr, ss = datetime.fromisoformat(sunrise), datetime.fromisoformat(sunset)
        now = datetime.now(sr.tzinfo)
        frac = min(1.0, max(0.0, (now - sr).total_seconds() / max(1.0, (ss - sr).total_seconds())))
    except Exception:
        pass
    ang = math.pi * (1 - frac)
    cx, rx = (left + right) / 2, (right - left) / 2
    sx = cx - rx * math.cos(ang)
    sy = base - ry * math.sin(ang)
    r = 9
    d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=BLACK)
    for a in range(0, 360, 45):  # sun rays
        rr = a * math.pi / 180
        d.line([sx + (r + 2) * math.cos(rr), sy + (r + 2) * math.sin(rr),
                sx + (r + 6) * math.cos(rr), sy + (r + 6) * math.sin(rr)], fill=BLACK, width=1)
    f = _font(18, False)
    d.text((left - 24, base + 4), (sunrise or "")[11:16], font=f, fill=BLACK)
    d.text((right - 22, base + 4), (sunset or "")[11:16], font=f, fill=BLACK)
    return img


def _temp_graph(w: int, times: List[str], temps: List[float]) -> Image.Image:
    temps = [t for t in temps if t is not None]
    if len(temps) < 2:
        return _blank(w, 1)
    h = 140
    img = _blank(w, h)
    d = ImageDraw.Draw(img)
    left, right, top, bot = 36, w - 12, 14, h - 26
    tmin, tmax = min(temps), max(temps)
    span = max(1.0, tmax - tmin)
    n = len(temps)

    def X(i):
        return left + (right - left) * i / (n - 1)

    def Y(t):
        return bot - (bot - top) * (t - tmin) / span

    d.line([left, top, left, bot], fill=BLACK, width=1)
    d.line([left, bot, right, bot], fill=BLACK, width=1)
    d.line([(X(i), Y(t)) for i, t in enumerate(temps)], fill=BLACK, width=2)
    f = _font(16, False)
    d.text((2, top - 3), f"{round(tmax)}C", font=f, fill=BLACK)
    d.text((2, bot - 9), f"{round(tmin)}C", font=f, fill=BLACK)
    for i in range(0, n, 6):
        hh = times[i][11:13] if i < len(times) else ""
        d.text((X(i) - 6, bot + 4), hh, font=f, fill=BLACK)
    return img


def render_brief(weather: dict, server_lines: List[str], greeting: str,
                 width: int = DEFAULT_WIDTH) -> Image.Image:
    w = width
    daily = weather.get("daily") or {}
    cur = weather.get("current") or {}
    hourly = weather.get("hourly") or {}

    def d0(key, default=None):
        v = daily.get(key)
        return v[0] if isinstance(v, list) and v else default

    parts = [
        _text_block(greeting, 40, True, "center", w),
        _text_block(time.strftime("%A, %d %B %Y"), TEXT_SIZE, False, "center", w),
        _rule_img(w),
        _text_block("BUCHAREST", 30, True, "center", w),
    ]
    if daily:
        desc = _WMO.get(d0("weather_code", -1), "")
        curt, hi, lo = cur.get("temperature_2m"), d0("temperature_2m_max"), d0("temperature_2m_min")
        parts.append(_text_block(desc, TEXT_SIZE, False, "center", w))
        parts.append(_text_block(f"now {curt}C   hi {hi}C   lo {lo}C", 24, False, "center", w))
        parts.append(_sun_arc(w, d0("sunrise", ""), d0("sunset", "")))
        parts.append(_temp_graph(w, hourly.get("time") or [], hourly.get("temperature_2m") or []))
        extra = []
        if d0("precipitation_probability_max") is not None:
            extra.append(f"rain {d0('precipitation_probability_max')}%")
        if d0("uv_index_max") is not None:
            extra.append(f"UV {round(d0('uv_index_max'))}")
        if d0("wind_speed_10m_max") is not None:
            extra.append(f"wind {round(d0('wind_speed_10m_max'))}km/h")
        if extra:
            parts.append(_text_block("   ".join(extra), 24, False, "center", w))
    else:
        parts.append(_text_block("weather unavailable", 24, False, "center", w))

    parts.append(_rule_img(w))
    parts.append(_text_block("SYSTEMS", 28, True, "left", w))
    for line in server_lines:
        parts.append(_text_block(line, 24, False, "left", w))
    parts.append(_rule_img(w))
    parts.append(_text_block("HAVE A NICE DAY", 32, True, "center", w))
    parts.append(_text_block("//// END TRANSMISSION ////", 18, False, "center", w))
    return _vstack(parts, w)
