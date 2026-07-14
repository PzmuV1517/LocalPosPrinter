import { useEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api'
import { useGuard } from '../common'

type Fmt = 'plain' | 'centered' | 'boxed' | 'header_body' | 'banner' | 'list' | 'barcode' | 'qrcode' | 'image' | 'alert'
type Vis = Partial<Record<'title' | 'text' | 'text_size' | 'barcode_type' | 'border_style' | 'items' | 'image' | 'alert_type' | 'service', boolean>>

const FIELD_VIS: Record<Fmt, Vis> = {
  plain: { text: true, text_size: true, image: true },
  centered: { text: true, text_size: true, image: true },
  boxed: { text: true, text_size: true, border_style: true, image: true },
  header_body: { title: true, text: true, text_size: true, image: true },
  banner: { title: true, text: true },
  list: { title: true, items: true, text_size: true },
  barcode: { title: true, text: true, barcode_type: true },
  qrcode: { title: true, text: true },
  image: { image: true },
  alert: { text: true, alert_type: true, service: true },
}
const FONT_FAMILY: Record<string, string> = { '1': 'monospace', '2': 'Jersey10', '3': 'Jacquard12', '4': 'Doto' }
const FMTS: Fmt[] = ['plain', 'centered', 'boxed', 'header_body', 'banner', 'list', 'barcode', 'qrcode', 'image', 'alert']
const SEVS = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug']
const BORDERS = ['line', 'dashes', 'equals', 'asterisk', 'at', 'hash', 'dot', 'plus', 'wave', 'box', 'double', 'rounded']

export function PrintTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [format, setFormat] = useState<Fmt>('plain')
  const [alertType, setAlertType] = useState('warning')
  const [service, setService] = useState('')
  const [printMode, setPrintMode] = useState('receipt')
  const [font, setFont] = useState('1')
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [textSize, setTextSize] = useState('')
  const [barcodeType, setBarcodeType] = useState('')
  const [borderStyle, setBorderStyle] = useState('line')
  const [items, setItems] = useState<{ label: string; value: string }[]>([{ label: 'Milk', value: 'x2' }, { label: 'Eggs', value: 'x12' }])
  const [imageB64, setImageB64] = useState<string | null>(null)
  const [imagePos, setImagePos] = useState('top')
  const [imgAdj, setImgAdj] = useState({ brightness: 1, contrast: 1, dither: 'fs', threshold: 128, invert: false, sharpen: false, auto: false })
  const [preview, setPreview] = useState('')
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null)
  const textRef = useRef<HTMLTextAreaElement>(null)

  const vis = FIELD_VIS[format]

  const payload = useMemo(() => {
    const p: Record<string, unknown> = { format, print_mode: printMode, font: parseInt(font, 10) }
    if (vis.title) p.title = title
    if (vis.text) p.text = text
    if (vis.text_size && textSize) p.text_size = parseInt(textSize, 10)
    if (vis.barcode_type) p.barcode_type = barcodeType
    if (vis.border_style) p.border_style = borderStyle
    if (vis.items) p.items = items.filter((it) => it.label || it.value)
    if (vis.alert_type) { p.alert_type = alertType; p.service = service; p.sent_at = Math.floor(Date.now() / 1000) }
    if (vis.image && imageB64) {
      p.image = imageB64
      p.image_position = imagePos
      p.image_brightness = imgAdj.brightness
      p.image_contrast = imgAdj.contrast
      p.image_dither = imgAdj.dither
      p.image_threshold = imgAdj.threshold
      p.image_invert = imgAdj.invert
      p.image_sharpen = imgAdj.sharpen
      p.image_autocontrast = imgAdj.auto
    }
    return p
  }, [format, printMode, font, title, text, textSize, barcodeType, borderStyle, items, alertType, service, imageB64, imagePos, imgAdj, vis])

  // Debounced live preview.
  useEffect(() => {
    const id = setTimeout(async () => {
      const r = await api.preview(payload)
      if (r.ok) setPreview(r.url); else setResult({ ok: false, text: r.error })
    }, 400)
    return () => clearTimeout(id)
  }, [payload])

  async function doPrint() {
    const res = await guard(api.printPayload(payload))
    if (!res) return
    const d = await res.json()
    setResult(res.ok ? { ok: true, text: d.message || 'Sent' } : { ok: false, text: d.error || 'Print failed' })
  }

  function onTextKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== 'Tab' || e.shiftKey) return
    e.preventDefault()
    const el = e.currentTarget, s = el.selectionStart, en = el.selectionEnd
    const next = text.slice(0, s) + '\t' + text.slice(en)
    setText(next)
    requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = s + 1 })
  }

  function onImage(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) { setImageB64(null); return }
    // Downscale in the browser to the print width before sending — keeps the payload tiny
    // (avoids reverse-proxy body limits) and normalizes to PNG (the server re-dithers anyway).
    const r = new FileReader()
    r.onload = () => {
      const img = new Image()
      img.onload = () => {
        const maxW = 384
        const scale = img.width > maxW ? maxW / img.width : 1
        const cw = Math.max(1, Math.round(img.width * scale))
        const ch = Math.max(1, Math.round(img.height * scale))
        const canvas = document.createElement('canvas')
        canvas.width = cw; canvas.height = ch
        const ctx = canvas.getContext('2d')
        if (!ctx) { setResult({ ok: false, text: 'Canvas not available' }); return }
        ctx.drawImage(img, 0, 0, cw, ch)
        setImageB64(canvas.toDataURL('image/png'))
      }
      img.onerror = () => setResult({ ok: false, text: 'Could not read that image — try a PNG or JPEG (HEIC isn’t supported by the browser).' })
      img.src = r.result as string
    }
    r.readAsDataURL(f)
  }

  return (
    <div className="two">
      <div className="col">
        <div className="card">
          <h2>Compose</h2>
          <label>Format</label>
          <select value={format} onChange={(e) => setFormat(e.target.value as Fmt)}>{FMTS.map((f) => <option key={f} value={f}>{f}</option>)}</select>

          {vis.alert_type && <><label>Alert type (severity)</label>
            <select value={alertType} onChange={(e) => setAlertType(e.target.value)}>{SEVS.map((s) => <option key={s} value={s}>{s}</option>)}</select></>}
          {vis.service && <><label>Issuing service</label><input value={service} placeholder="e.g. backup.service" onChange={(e) => setService(e.target.value)} /></>}

          <label>Print mode</label>
          <select value={printMode} onChange={(e) => setPrintMode(e.target.value)}><option value="receipt">Receipt</option><option value="label">Label</option></select>

          <label>Font (all formats)</label>
          <select value={font} onChange={(e) => setFont(e.target.value)}>
            <option value="1">1 — Mono (default)</option><option value="2">2 — Jersey 10</option>
            <option value="3">3 — Jacquard 12</option><option value="4">4 — Doto</option>
          </select>
          <div className="muted" style={{ fontSize: 20, marginTop: 6, fontFamily: FONT_FAMILY[font] }}>Sample — ALERT 0123</div>

          {vis.title && <><label>Title</label><input value={title} placeholder="Title" onChange={(e) => setTitle(e.target.value)} /></>}
          {vis.text && <><label>Text</label>
            <textarea ref={textRef} value={text} placeholder="Text to print… (Tab inserts a tab)" onChange={(e) => setText(e.target.value)} onKeyDown={onTextKey} /></>}
          {vis.text_size && <><label>Text size (px)</label><input type="number" min={10} max={120} value={textSize} placeholder="26" onChange={(e) => setTextSize(e.target.value)} /></>}
          {vis.barcode_type && <><label>Barcode type</label><input value={barcodeType} placeholder="CODE128, EAN13, UPC_A, CODE39, ITF" onChange={(e) => setBarcodeType(e.target.value)} /></>}
          {vis.border_style && <><label>Border style</label>
            <select value={borderStyle} onChange={(e) => setBorderStyle(e.target.value)}>{BORDERS.map((b) => <option key={b} value={b}>{b}</option>)}</select></>}

          {vis.items && <>
            <label>List items (label / value)</label>
            {items.map((it, i) => (
              <div className="items-row" key={i}>
                <input value={it.label} placeholder="label" onChange={(e) => setItems(items.map((x, j) => j === i ? { ...x, label: e.target.value } : x))} />
                <input value={it.value} placeholder="value" onChange={(e) => setItems(items.map((x, j) => j === i ? { ...x, value: e.target.value } : x))} />
                <button className="ghost mini" style={{ flex: '0 0 auto' }} onClick={() => setItems(items.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
            <button className="ghost mini" onClick={() => setItems([...items, { label: '', value: '' }])}>+ Add row</button>
          </>}

          {vis.image && <>
            <label>Image (optional for text; required for Image)</label>
            <input type="file" accept="image/*" onChange={onImage} />
            <label>Image position</label>
            <select value={imagePos} onChange={(e) => setImagePos(e.target.value)}><option value="top">Top</option><option value="bottom">Bottom</option></select>
            {imageB64 && <>
              <label style={{ marginTop: 12 }}>Image adjustments (fix faint prints)</label>
              <div className="row">
                <div><label>Brightness {imgAdj.brightness.toFixed(2)}</label>
                  <input type="range" min={0.3} max={2} step={0.05} value={imgAdj.brightness}
                    onChange={(e) => setImgAdj({ ...imgAdj, brightness: +e.target.value })} /></div>
                <div><label>Contrast {imgAdj.contrast.toFixed(2)}</label>
                  <input type="range" min={0.3} max={3} step={0.05} value={imgAdj.contrast}
                    onChange={(e) => setImgAdj({ ...imgAdj, contrast: +e.target.value })} /></div>
              </div>
              <div className="row" style={{ alignItems: 'flex-end' }}>
                <div><label>Mode</label>
                  <select value={imgAdj.dither} onChange={(e) => setImgAdj({ ...imgAdj, dither: e.target.value })}>
                    <option value="fs">Dither (photos)</option>
                    <option value="threshold">Threshold (logos/line art)</option>
                  </select></div>
                {imgAdj.dither === 'threshold' &&
                  <div><label>Threshold {imgAdj.threshold}</label>
                    <input type="range" min={16} max={240} step={4} value={imgAdj.threshold}
                      onChange={(e) => setImgAdj({ ...imgAdj, threshold: +e.target.value })} /></div>}
              </div>
              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginTop: 8 }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, margin: 0, textTransform: 'none' }}>
                  <input type="checkbox" checked={imgAdj.auto} onChange={(e) => setImgAdj({ ...imgAdj, auto: e.target.checked })} style={{ width: 'auto' }} /> auto-contrast</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, margin: 0, textTransform: 'none' }}>
                  <input type="checkbox" checked={imgAdj.sharpen} onChange={(e) => setImgAdj({ ...imgAdj, sharpen: e.target.checked })} style={{ width: 'auto' }} /> sharpen</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, margin: 0, textTransform: 'none' }}>
                  <input type="checkbox" checked={imgAdj.invert} onChange={(e) => setImgAdj({ ...imgAdj, invert: e.target.checked })} style={{ width: 'auto' }} /> invert</label>
                <button className="ghost mini" onClick={() => setImgAdj({ brightness: 1, contrast: 1, dither: 'fs', threshold: 128, invert: false, sharpen: false, auto: false })}>reset</button>
              </div>
            </>}
          </>}

          <div className="row" style={{ marginTop: 16 }}>
            <button className="ghost" onClick={doPrint}>Print</button>
          </div>
          {result && <div className={`result ${result.ok ? 'ok' : 'bad'}`}>{result.text}</div>}
        </div>
      </div>
      <div className="col">
        <label style={{ marginTop: 0 }}>Live preview — pixel-for-pixel what prints</label>
        <div className="preview-box">{preview && <img src={preview} alt="preview" />}</div>
      </div>
    </div>
  )
}
