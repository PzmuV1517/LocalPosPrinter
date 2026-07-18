import { useEffect, useMemo, useRef, useState } from 'react'
import { hud } from './Hud'

let lastRenderer = ''

// Graphics-acceleration gate: a software / missing WebGL renderer means no intro (render flat and
// instant). SVG filters ride the GPU, so WebGL presence is a fair proxy for "acceleration is on".
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

const OPEN_MS = 570  // blank + line + bloom-open, matches the CSS animation timings
const FADE = 5000    // fisheye + scanline fade after it opens
const WARP = 95      // peak fisheye displacement, px

// Barrel-distortion displacement map: R/G encode an inward radial offset that grows quadratically
// toward the edges, so the middle bulges toward the viewer like a CRT tube.
function warpMap(size = 96, k = 0.25): string {
  const c = document.createElement('canvas')
  c.width = c.height = size
  const g = c.getContext('2d')!
  const im = g.createImageData(size, size)
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const nx = (x / (size - 1)) * 2 - 1
      const ny = (y / (size - 1)) * 2 - 1
      const r2 = nx * nx + ny * ny
      const i = (y * size + x) * 4
      im.data[i] = Math.max(0, Math.min(255, (0.5 - k * nx * r2) * 255))
      im.data[i + 1] = Math.max(0, Math.min(255, (0.5 - k * ny * r2) * 255))
      im.data[i + 2] = 128
      im.data[i + 3] = 255
    }
  }
  g.putImageData(im, 0, 0)
  return c.toDataURL()
}

/**
 * One-shot CRT power-on over its children, run entirely on the LIVE DOM so the page stays
 * navigable throughout: a GPU-composited SVG barrel filter for the fisheye, plus click-through
 * overlays for the centre-line bloom, flash and fading scanlines.
 */
export function CrtBoot({ active, children }: { active: boolean; children: React.ReactNode }) {
  const canPlay = useMemo(() => accelerated(), [])
  const [trigger, setTrigger] = useState(active ? 1 : 0)
  const [running, setRunning] = useState(active && canPlay)
  const map = useMemo(() => (canPlay ? warpMap() : ''), [canPlay])
  const disp = useRef<SVGFEDisplacementMapElement>(null)

  useEffect(() => {
    const h = () => setTrigger((t) => t + 1)
    window.addEventListener('crt-replay', h)
    return () => window.removeEventListener('crt-replay', h)
  }, [])

  useEffect(() => {
    if (trigger === 0) return
    if (!canPlay) { console.warn('[boot] no GPU accel', lastRenderer); hud(`boot: no GPU accel (${lastRenderer})`, 6000); return }
    setRunning(true)
    let raf = 0
    const t0 = performance.now()
    const total = OPEN_MS + FADE
    const tick = (now: number) => {
      const e = now - t0
      const s = e < OPEN_MS ? WARP : Math.max(0, WARP * (1 - (e - OPEN_MS) / FADE))
      disp.current?.setAttribute('scale', String(s))
      if (e < total) raf = requestAnimationFrame(tick)
      else setRunning(false)
    }
    raf = requestAnimationFrame(tick)
    const safety = window.setTimeout(() => setRunning(false), total + 1000)
    return () => { cancelAnimationFrame(raf); clearTimeout(safety) }
  }, [trigger, canPlay])

  return (
    <>
      {running && (
        <svg className="crt-svg" aria-hidden="true">
          <filter id="crtWarp" x="-15%" y="-15%" width="130%" height="130%">
            <feImage href={map} result="m" preserveAspectRatio="none" x="0" y="0" width="100%" height="100%" />
            <feDisplacementMap ref={disp} in="SourceGraphic" in2="m" scale={WARP}
              xChannelSelector="R" yChannelSelector="G" />
          </filter>
        </svg>
      )}
      <div className="crt-wrap" style={running ? { filter: 'url(#crtWarp)' } : undefined}>
        {children}
      </div>
      {running && (
        <>
          <div className="crt-bar top" />
          <div className="crt-bar bot" />
          <div className="crt-scan" />
          <div className="crt-glow" />
          <div className="crt-flash" />
          <div className="crt-line" />
        </>
      )}
    </>
  )
}
