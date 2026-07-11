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
import os
import random
import re
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

# ---- layout constants (kept aligned with the Android renderer) ----
DEFAULT_WIDTH = int(os.environ.get("PRINT_WIDTH", "384"))
PAD = 12
TEXT_SIZE = 26
TITLE_SIZE = 40
BORDER = 3
DIVIDER = 2
TAB_WIDTH = 4
MIN_TEXT_SIZE = 10
MAX_TEXT_SIZE = 120

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
# Fonts — one explicit bundled TTF for determinism regardless of host OS.
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


def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    path = _BOLD_PATH if bold else _REGULAR_PATH
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


def _text_block(text: str, size: int, bold: bool, align: str, w: int, pad: int = PAD) -> Image.Image:
    font = _font(size, bold)
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
# Plain repeating-character primitives — nothing copied from any art collection.
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


def _mono_block(lines: List[str], w: int, size: int = TEXT_SIZE) -> Image.Image:
    """Draw pre-formatted monospace lines verbatim (no re-wrapping / space collapsing)."""
    font = _font(size, False)
    lh = _line_height(font)
    img = _blank(w, lh * max(1, len(lines)) + 2 * PAD)
    draw = ImageDraw.Draw(img)
    y = PAD
    for line in lines:
        draw.text((PAD, y), line, font=font, fill=BLACK)
        y += lh
    return img


def _ascii_boxed(text: str, w: int, style: str, size: int = TEXT_SIZE) -> Image.Image:
    border = BORDERS.get(style)
    if border is None:
        return _boxed(text, w, size)  # unknown style -> fall back to the drawn rectangle
    font = _font(size, False)
    scratch = ImageDraw.Draw(_blank(1, 1))
    cw = _text_width(scratch, "M", font) or 1
    cols = int((w - 2 * PAD) // cw)
    if cols < 5:
        return _boxed(text, w, size)
    inner = cols - 2
    tl, hz, tr, vt, bl, br = border
    lines = [tl + hz * inner + tr]
    for para in (text or "").split("\n"):
        for wl in _wrap_chars(para, inner):
            lines.append(vt + wl.ljust(inner) + vt)
    lines.append(bl + hz * inner + br)
    return _mono_block(lines, w, size)


def _boxed(text: str, w: int, size: int = TEXT_SIZE) -> Image.Image:
    pad = PAD + BORDER + 6
    inner = _text_block(text, size, False, "left", w, pad=pad)
    draw = ImageDraw.Draw(inner)
    half = BORDER // 2
    draw.rectangle(
        [half, half, w - 1 - half, inner.height - 1 - half],
        outline=BLACK, width=BORDER,
    )
    return inner


def _header_body(title: Optional[str], body: str, w: int, size: int = TEXT_SIZE) -> Image.Image:
    parts = []
    if title:
        parts.append(_text_block(title, _title_size(size), True, "center", w))
        parts.append(_divider(w))
    parts.append(_text_block(body, size, False, "left", w))
    return _stack(parts, w)


def _banner(text: str, w: int) -> Image.Image:
    inner = max(1, w - 2 * PAD)
    scratch = ImageDraw.Draw(_blank(1, 1))
    # Grow the font until the widest *whole word* no longer fits on a line. Using the
    # widest word (not char-broken lines) avoids runaway scaling on long strings.
    words = text.split() or [text]
    chosen = 30
    size = 30
    while size <= 160:
        font = _font(size, True)
        widest_word = max(_text_width(scratch, wd, font) for wd in words)
        if widest_word > inner:
            break
        chosen = size
        size += 4
    return _text_block(text, chosen, True, "center", w)


def _list_format(payload: dict, w: int, size: int = TEXT_SIZE) -> Image.Image:
    parts = []
    title = payload.get("title")
    if title:
        parts.append(_text_block(title, _title_size(size), True, "center", w))
        parts.append(_divider(w))
    font = _font(size, False)
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
        img = Image.open(io.BytesIO(_decode_b64(raw)))
        img = img.convert("1")           # printed as-is, no re-dither
        return _scale_to_width(img, w) if img.width != w else img
    std = payload.get("image")
    if std:
        img = Image.open(io.BytesIO(_decode_b64(std))).convert("L")
        img = _scale_to_width(img, w)
        return img.convert("1")          # Floyd–Steinberg (Pillow default dither)
    return None


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

    if fmt == "image":
        img = _image_field(payload, w)
        if img is None:
            raise RenderError("format 'image' requires 'image' or 'image_raw_bitmap'")
        return img

    if fmt == "barcode":
        text = payload.get("text")
        if not text:
            raise RenderError("barcode requires 'text'")
        content = _barcode(text, payload.get("barcode_type"), w)
        return _with_optional_title(payload, w, content)

    if fmt == "qrcode":
        text = payload.get("text")
        if not text:
            raise RenderError("qrcode requires 'text'")
        content = _qrcode(text, w)
        return _with_optional_title(payload, w, content)

    # text formats: honour a text_size override and expand tabs so whitespace prints as typed.
    size = _clamp_size(payload.get("text_size"))
    raw = (payload.get("text") or "").expandtabs(TAB_WIDTH)
    text = expand_tags(raw, w, size)
    if fmt == "centered":
        text_img = _text_block(text, size, False, "center", w)
    elif fmt == "boxed":
        style = (payload.get("border_style") or "line").lower()
        text_img = _boxed(text, w, size) if style in ("line", "") else _ascii_boxed(text, w, style, size)
    elif fmt == "header_body":
        text_img = _header_body(payload.get("title"), text, w, size)
    elif fmt == "banner":
        text_img = _banner(payload.get("title") or text, w)  # banner size is auto-scaled
    elif fmt == "list":
        text_img = _list_format(payload, w, size)
    else:  # plain / unknown
        text_img = _text_block(text, size, False, "left", w)

    img = _image_field(payload, w)
    if img is None:
        return text_img
    position = (payload.get("image_position") or "top").lower()
    parts = [text_img, img] if position == "bottom" else [img, text_img]
    return _stack(parts, w)


def _with_optional_title(payload: dict, w: int, content: Image.Image) -> Image.Image:
    title = payload.get("title")
    if not title:
        return content
    return _stack([_text_block(title, TITLE_SIZE, True, "center", w), _divider(w), content], w)


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def to_base64_png(img: Image.Image) -> str:
    return base64.b64encode(to_png_bytes(img)).decode("ascii")
