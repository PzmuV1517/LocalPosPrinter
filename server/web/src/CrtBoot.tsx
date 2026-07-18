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

const BLANK = 150   // ms of blank screen before the power-on flash
const FLASH = 350   // ms flash + reveal
const FADE = 5000   // ms fisheye + scanline fade after the flash
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
    let cancelled = false
    let app: any = null
    let canvas: HTMLCanvasElement | null = null
    const cleanup = () => {
      try { app?.destroy(true, { children: true, texture: true }) } catch { /* already gone */ }
      canvas?.remove()
    }

    void (async () => {
      try {
        const node = wrap.current
        if (!node) throw new Error('no node')
        const [{ Application, Sprite, Texture }, { CRTFilter }, { toCanvas }] = await Promise.all([
          import('pixi.js'), import('pixi-filters'), import('html-to-image'),
        ])
        const snap = await toCanvas(node, {
          width: window.innerWidth, height: window.innerHeight,
          pixelRatio: Math.min(2, window.devicePixelRatio || 1),
        })
        if (cancelled) return

        app = new Application()
        await app.init({ background: 0x000000, resizeTo: window, antialias: true })
        if (cancelled) { cleanup(); return }
        canvas = app.canvas as HTMLCanvasElement
        Object.assign(canvas.style, {
          position: 'fixed', top: '0', left: '0', width: '100vw', height: '100vh', zIndex: '9999',
        })
        document.body.appendChild(canvas)

        const sprite = new Sprite(Texture.from(snap))
        sprite.alpha = 0
        const crt = new CRTFilter({
          curvature: CURVE, lineWidth: 3, lineContrast: CONTRAST, noise: NOISE,
          noiseSize: 1, vignetting: 0.5, vignettingAlpha: 1, time: 0,
        })
        sprite.filters = [crt]
        const flash = new Sprite(Texture.WHITE)
        flash.alpha = 0
        app.stage.addChild(sprite)
        app.stage.addChild(flash)

        const t0 = performance.now()
        app.ticker.add(() => {
          const w = app.screen.width, h = app.screen.height
          sprite.width = w; sprite.height = h
          flash.width = w; flash.height = h
          crt.time += 0.5
          const e = performance.now() - t0
          if (e < BLANK) {
            sprite.alpha = 0; flash.alpha = 0
          } else if (e < BLANK + FLASH) {
            const p = (e - BLANK) / FLASH
            sprite.alpha = Math.min(1, p * 2)
            flash.alpha = Math.sin(p * Math.PI)
          } else {
            sprite.alpha = 1; flash.alpha = 0
            const k = 1 - Math.min(1, (e - BLANK - FLASH) / FADE)
            crt.curvature = CURVE * k
            crt.lineContrast = CONTRAST * k
            crt.vignettingAlpha = k
            crt.noise = NOISE * k
            if (k <= 0 && !cancelled) { cancelled = true; cleanup(); setRunning(false) }
          }
        })
      } catch (err) {
        console.error('[boot] intro failed', err)
        hud('boot failed: ' + ((err as Error)?.message || String(err)), 8000)
        cleanup(); setRunning(false)
      }
    })()

    return () => { cancelled = true; cleanup() }
  }, [trigger, canPlay])

  return (
    <>
      <div ref={wrap} className="crt-wrap">{children}</div>
      {running && <div className="crt-blank" />}
    </>
  )
}
