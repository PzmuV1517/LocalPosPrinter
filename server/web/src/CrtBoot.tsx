import { useEffect, useMemo, useRef, useState } from 'react'
import { hud } from './Hud'

let lastRenderer = ''

// Graphics-acceleration gate: a software / missing WebGL renderer means no intro (render flat and
// instant). Pixi needs a real GPU to look right, so this is the switch.
function accelerated(): boolean {
  try {
    const gl = document.createElement('canvas').getContext('webgl') as WebGLRenderingContext | null
    if (!gl) { lastRenderer = 'no-webgl'; return false }
    const dbg = gl.getExtension('WEBGL_debug_renderer_info')
    lastRenderer = dbg ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) : 'unknown'
    const ok = !/swiftshader|llvmpipe|software|basic render|paravirtual/i.test(lastRenderer)
    console.log('[boot] renderer=%o accelerated=%o', lastRenderer, ok)
    return ok
  } catch {
    lastRenderer = 'probe-error'
    return false
  }
}

// Fire from anywhere (e.g. the Settings test button) to replay the intro.
export const replayCrt = () => window.dispatchEvent(new Event('crt-replay'))

const BLANK = 120   // ms black before the tube fires
const LINE = 150    // ms the bright centre line builds
const OPEN = 300    // ms the image blooms open from the line
const FADE = 5000   // ms fisheye + scanline fade after it opens
const CURVE = 9     // peak CRT curvature (fisheye)
const NOISE = 0.18
const CONTRAST = 0.4

/**
 * One-shot CRT power-on over its children: snapshots the screen, runs it through Pixi's CRTFilter
 * (curvature/scanlines/vignette) with a flash, fades over 5s, then reveals the live DOM. Heavy libs
 * load on demand. Any failure reveals the DOM flat. Corner badge is a temporary debug readout.
 */
export function CrtBoot({ active, children }: { active: boolean; children: React.ReactNode }) {
  const canPlay = useMemo(() => accelerated(), [])
  const [trigger, setTrigger] = useState(active ? 1 : 0)
  const [running, setRunning] = useState(active && canPlay)
  const wrap = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const h = () => setTrigger((t) => t + 1)
    window.addEventListener('crt-replay', h)
    return () => window.removeEventListener('crt-replay', h)
  }, [])

  useEffect(() => {
    if (trigger === 0) return
    if (!canPlay) { console.warn('[boot] no GPU accel', lastRenderer); hud(`boot: no GPU accel (${lastRenderer})`, 6000); return }
    setRunning(true)
    const sx0 = window.scrollX, sy0 = window.scrollY
    let cancelled = false
    let app: any = null
    let canvas: HTMLCanvasElement | null = null
    let safety = 0
    const finish = () => {
      if (cancelled) return
      cancelled = true
      clearTimeout(safety)
      try { app?.destroy(true, { children: true, texture: true }) } catch { /* already gone */ }
      canvas?.remove()
      setRunning(false)
    }

    void (async () => {
      try {
        const node = wrap.current
        if (!node) throw new Error('no node')
        const [pixi, { CRTFilter }, { toCanvas }] = await Promise.all([
          import('pixi.js'), import('pixi-filters'), import('html-to-image'),
        ])
        // Strict CSP blocks eval; this side-effect module swaps Pixi to no-eval shader generation.
        // Must load before the renderer is created.
        await import('pixi.js/unsafe-eval')
        const { Application, Sprite, Texture, Graphics } = pixi
        // Snapshot the CURRENT viewport (shift the clone up by the scroll offset) so revealing the
        // live DOM at the end lines up exactly, no jump.
        const snap = await toCanvas(node, {
          width: window.innerWidth, height: window.innerHeight,
          pixelRatio: Math.min(2, window.devicePixelRatio || 1),
          style: { transform: `translate(${-sx0}px, ${-sy0}px)`, transformOrigin: 'top left' },
        })
        if (cancelled) return
        window.scrollTo(sx0, sy0)  // html-to-image can nudge scroll during capture, put it back

        app = new Application()
        await app.init({ background: 0x000000, resizeTo: window, antialias: true })
        if (cancelled) { finish(); return }
        canvas = app.canvas as HTMLCanvasElement
        // pointer-events none: the live page behind stays fully clickable during the intro.
        Object.assign(canvas.style, {
          position: 'fixed', top: '0', left: '0', width: '100vw', height: '100vh',
          zIndex: '9999', pointerEvents: 'none',
        })
        document.body.appendChild(canvas)
        // Hard stop: whatever happens, tear the overlay down so the page can never get stuck.
        safety = window.setTimeout(finish, BLANK + LINE + OPEN + FADE + 1500)

        const sprite = new Sprite(Texture.from(snap))
        const crt = new CRTFilter({
          curvature: CURVE, lineWidth: 3, lineContrast: CONTRAST, noise: NOISE,
          noiseSize: 1, vignetting: 0.5, vignettingAlpha: 1, time: 0,
        })
        sprite.filters = [crt]
        const mask = new Graphics()   // reveals the image, grows from the centre line
        sprite.mask = mask
        const line = new Sprite(Texture.WHITE)  // the bright power-on line
        line.alpha = 0
        const flash = new Sprite(Texture.WHITE) // bloom
        flash.alpha = 0
        app.stage.addChild(sprite, mask, line, flash)

        const t0 = performance.now()
        app.ticker.add(() => {
          try {
            const w = app.screen.width, h = app.screen.height
            sprite.width = w; sprite.height = h
            flash.width = w; flash.height = h
            crt.time += 0.5
            const e = performance.now() - t0

            if (e < BLANK) {
              mask.clear(); line.alpha = 0; flash.alpha = 0
            } else if (e < BLANK + LINE) {
              const p = (e - BLANK) / LINE
              line.width = w; line.height = 3; line.x = 0; line.y = h / 2 - 1.5; line.alpha = p
              mask.clear().rect(0, h / 2 - 2, w, 4).fill(0xffffff)
              flash.alpha = 0
            } else if (e < BLANK + LINE + OPEN) {
              const p = (e - BLANK - LINE) / OPEN
              const band = Math.max(4, h * (1 - (1 - p) * (1 - p)))
              line.width = w; line.height = 3; line.x = 0; line.y = h / 2 - 1.5; line.alpha = 1 - p
              mask.clear().rect(0, h / 2 - band / 2, w, band).fill(0xffffff)
              flash.alpha = Math.sin(p * Math.PI) * 0.6
            } else {
              if (sprite.mask) { sprite.mask = null; mask.visible = false; line.visible = false }
              flash.alpha = 0
              const k = 1 - Math.min(1, (e - BLANK - LINE - OPEN) / FADE)
              crt.curvature = CURVE * k
              crt.lineContrast = CONTRAST * k
              crt.vignettingAlpha = k
              crt.noise = NOISE * k
              if (k <= 0) finish()
            }
          } catch (err) {
            console.error('[boot] tick error', err)
            finish()
          }
        })
      } catch (err) {
        console.error('[boot] intro failed', err)
        hud('boot failed: ' + ((err as Error)?.message || String(err)), 8000)
        finish()
      }
    })()

    return () => { finish() }
  }, [trigger, canPlay])

  return (
    <>
      <div ref={wrap} className="crt-wrap">{children}</div>
      {running && <div className="crt-blank" />}
    </>
  )
}
