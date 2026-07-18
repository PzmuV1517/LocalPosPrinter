import { useEffect, useMemo, useRef, useState } from 'react'

// Graphics-acceleration gate: reduced-motion, or a software / missing WebGL renderer, means no
// intro (render flat and instant). Pixi needs real GPU to look right, so this is the switch.
function accelerated(): boolean {
  if (typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches) return false
  try {
    const gl = document.createElement('canvas').getContext('webgl') as WebGLRenderingContext | null
    if (!gl) return false
    const dbg = gl.getExtension('WEBGL_debug_renderer_info')
    const r = dbg ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) : ''
    return !/swiftshader|llvmpipe|software|basic render|paravirtual/i.test(r)
  } catch {
    return false
  }
}

const BLANK = 150   // ms of blank screen before the power-on flash
const FLASH = 350   // ms flash + reveal
const FADE = 5000   // ms fisheye + scanline fade after the flash
const CURVE = 9     // peak CRT curvature (fisheye)
const NOISE = 0.18
const CONTRAST = 0.4

/**
 * One-shot CRT power-on over its children: snapshots the screen, runs it through Pixi's CRTFilter
 * (curvature/scanlines/vignette) with a flash, fades over 5s, then reveals the live DOM. The heavy
 * libs are loaded on demand, so only a fresh login pays for them. Any failure reveals the DOM flat.
 */
export function CrtBoot({ active, children }: { active: boolean; children: React.ReactNode }) {
  const enabled = useMemo(() => active && accelerated(), [active])
  const [running, setRunning] = useState(enabled)
  const wrap = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    let app: { destroy: (a: boolean, b: object) => void; canvas: HTMLCanvasElement; screen: { width: number; height: number }; stage: any; ticker: any; init: (o: object) => Promise<void> } | null = null
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
          pixelRatio: Math.min(2, window.devicePixelRatio || 1), cacheBust: true,
        })
        if (cancelled) return

        app = new Application() as unknown as typeof app
        await app!.init({ background: 0x000000, resizeTo: window, antialias: true })
        if (cancelled) { cleanup(); return }
        canvas = app!.canvas
        Object.assign(canvas.style, { position: 'fixed', inset: '0', zIndex: '9999' })
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
        app!.stage.addChild(sprite)
        app!.stage.addChild(flash)

        const t0 = performance.now()
        app!.ticker.add(() => {
          const w = app!.screen.width, h = app!.screen.height
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
      } catch {
        cleanup(); setRunning(false)
      }
    })()

    return () => { cancelled = true; cleanup() }
  }, [enabled])

  if (!enabled) return <>{children}</>
  return (
    <>
      <div ref={wrap} className="crt-wrap">{children}</div>
      {running && <div className="crt-blank" />}
    </>
  )
}
