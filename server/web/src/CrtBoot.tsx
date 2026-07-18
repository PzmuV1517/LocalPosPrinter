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

// Fake kernel/BIOS boot log that scrolls past after the flash (desktop only).
const BOOT_LINES = `WATCHTOWER BIOS v2.3.4   (c) PzmuV1517
POST ....................... OK
CPU: phosphor-core @ 15.7kHz  [1 tube]
Memory test: 65536 lines ... OK
Detecting scanline controller ... found
Initializing electron gun ...... ready
[    0.000000] Booting WATCHTOWER kernel
[    0.001132] Calibrating deflection yoke
[    0.004071] pid_max: default 32768
[    0.006553] Mounting /dev/crt0
[    0.009210] phosphor: P1 persistence 12ms
[    0.013887] Loading printer daemon ...... ok
[    0.018402] relay: opening socket
[    0.021995] relay: link up
[    0.026110] confer: encrypting channels
[    0.031264] confer: 4 rooms ready
[    0.037781] mqtt: bridge listening
[    0.042013] scout: watchdog armed
[    0.048662] weather: open-meteo handshake
[    0.055120] brief: 366 quotes indexed
[    0.061330] auth: scrypt ready
[    0.067884] tls: HSTS enforced
[    0.074001] net: HMAC identity verified
[    0.081559] fs: journald forwarding online
[    0.090114] input: touchscreen [absent]
[    0.101772] display: 1 CRT, fisheye enabled
[  ok  ] Started phosphor warm-up
[  ok  ] Reached target Print Services
[  ok  ] Reached target Confer
[  ok  ] Reached target Multi-User System
starting session for operator ...
decrypting vault .......... ok
loading dashboard modules
  logs .......... ok
  print ......... ok
  devices ....... ok
  passwords ..... ok
  history ....... ok
  settings ...... ok
handshake complete.
WATCHTOWER online.`

const OPEN_MS = 570  // blank + line + bloom-open, matches the CSS animation timings
const FADE = 5000    // fisheye + scanline fade after it opens
const WARP = 78      // peak fisheye displacement, px

// Barrel/fisheye displacement map. Built at the screen's aspect ratio and measured radially in
// real screen space (normalised by the shorter half-axis), so the bulge is a true circle, not a
// cylinder. R/G encode an inward offset growing with radius^2, so the centre magnifies like a lens.
function warpMap(): string {
  const aspect = (window.innerWidth || 1) / (window.innerHeight || 1)
  const long = 160
  const w = aspect >= 1 ? long : Math.max(2, Math.round(long * aspect))
  const h = aspect >= 1 ? Math.max(2, Math.round(long / aspect)) : long
  const half = Math.min(w, h) / 2 || 1
  const cx = (w - 1) / 2, cy = (h - 1) / 2
  const rx = new Float32Array(w * h), ry = new Float32Array(w * h)
  let peak = 1e-6
  for (let j = 0; j < h; j++) {
    for (let i = 0; i < w; i++) {
      const nx = (i - cx) / half, ny = (j - cy) / half
      const rn = Math.hypot(nx, ny)
      const ax = -nx * rn, ay = -ny * rn   // inward, magnitude grows with radius^2
      const k = j * w + i
      rx[k] = ax; ry[k] = ay
      peak = Math.max(peak, Math.abs(ax), Math.abs(ay))
    }
  }
  const c = document.createElement('canvas'); c.width = w; c.height = h
  const g = c.getContext('2d')!
  const im = g.createImageData(w, h)
  for (let k = 0; k < w * h; k++) {
    im.data[k * 4] = Math.max(0, Math.min(255, (0.5 + 0.5 * rx[k] / peak) * 255))
    im.data[k * 4 + 1] = Math.max(0, Math.min(255, (0.5 + 0.5 * ry[k] / peak) * 255))
    im.data[k * 4 + 2] = 128
    im.data[k * 4 + 3] = 255
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
  const map = useMemo(() => (canPlay ? warpMap() : ''), [canPlay, trigger])
  const disp = useRef<SVGFEDisplacementMapElement>(null)
  // Boot log is desktop-only (skip phones).
  const desktop = typeof window !== 'undefined' && window.matchMedia('(min-width: 820px)').matches

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
          <filter id="crtWarp" x="0" y="0" width="100%" height="100%" colorInterpolationFilters="sRGB">
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
          <div className="crt-flash" />
          <div className="crt-line" />
          {desktop && (
            <div className="crt-boot" aria-hidden="true">
              <pre className="crt-boot-roll">{BOOT_LINES}</pre>
            </div>
          )}
        </>
      )}
    </>
  )
}
