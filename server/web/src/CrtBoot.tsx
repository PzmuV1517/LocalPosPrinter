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

// Fake kernel/BIOS boot log typed out after the flash (desktop only).
const BOOT_LINES = `WATCHTOWER BIOS v2.3.4   (c) PzmuV1517
POST ....................... OK
CPU: phosphor-core @ 15.7kHz  [1 tube]
L1 cache 32K  L2 cache 256K  ok
Memory test: 65536 lines ... OK
Detecting scanline controller ... found
Initializing electron gun ...... ready
Aperture grille aligned  dot pitch 0.24
[    0.000000] Booting WATCHTOWER kernel 2.3.4
[    0.000381] Command line: ro quiet phosphor=green
[    0.001132] Calibrating deflection yoke
[    0.002007] Calibrating delay loop ... 4390.14 BogoMIPS
[    0.003118] hpet: 3 channels
[    0.004071] pid_max: default 32768
[    0.005002] Mount-cache hash table entries: 2048
[    0.006553] Mounting /dev/crt0
[    0.007889] devtmpfs: initialized
[    0.009210] phosphor: P1 persistence 12ms
[    0.010774] clocksource: refresh 60.00Hz
[    0.012330] NET: Registered protocol family 2
[    0.013887] Loading printer daemon ......... ok
[    0.015540] usb 1-1: Sunmi thermal head detected
[    0.017001] printer: 384px width, 203dpi
[    0.018402] relay: opening socket
[    0.019995] relay: TLS handshake ... ok
[    0.021995] relay: link up
[    0.023660] relay: registered target 'default'
[    0.026110] confer: encrypting channels
[    0.028004] confer: SecretBox keys derived
[    0.031264] confer: 4 rooms ready
[    0.034120] mqtt: starting embedded broker
[    0.037781] mqtt: bridge listening 0.0.0.0:1883
[    0.039900] mqtt: discovery published
[    0.042013] scout: watchdog armed
[    0.044550] scout: 2/3 agents reporting
[    0.048662] weather: open-meteo handshake
[    0.051200] weather: Bucharest 44.43N 26.10E
[    0.055120] brief: 366 quotes indexed
[    0.058330] brief: sunrise 05:47  sunset 21:01
[    0.061330] auth: scrypt N=16384 r=8 p=1
[    0.064110] auth: session store online
[    0.067884] tls: HSTS enforced  preload
[    0.070550] tls: base-uri locked
[    0.074001] net: HMAC identity verified
[    0.077230] net: rate limiter armed
[    0.081559] fs: journald forwarding online
[    0.085004] fs: WAL checkpoint ok
[    0.090114] input: touchscreen [absent]
[    0.094880] input: keyboard [ps/2]
[    0.101772] display: 1 CRT, fisheye enabled
[    0.106540] display: barrel k=0.25  warp=78
[    0.112000] battery: 82%  charging
[  ok  ] Started phosphor warm-up
[  ok  ] Started Printer Daemon
[  ok  ] Started Relay Link
[  ok  ] Started MQTT Bridge
[  ok  ] Started Scout Watchdog
[  ok  ] Reached target Network
[  ok  ] Reached target Print Services
[  ok  ] Reached target Confer
[  ok  ] Reached target Multi-User System
starting session for operator ...
verifying token ........... ok
decrypting vault .......... ok
sync clock: 07:00 EET
loading dashboard modules
  logs .......... ok
  print ......... ok
  confer ........ ok
  devices ....... ok
  passwords ..... ok
  history ....... ok
  settings ...... ok
mounting live feed ........ ok
subscribing to relay ...... ok
warming caches ............ ok
handshake complete.
all systems nominal.
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

// Real terminal: types BOOT_LINES out character by character, bottom-anchored so it scrolls up
// like a console as it fills. Starts just as the reveal bars open.
function BootLog() {
  const [n, setN] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    // Finish typing well before the end, then fade the whole log out (no abrupt pop).
    const START = 250, TYPE_END = OPEN_MS + FADE - 900, FADE_OUT = 800
    const perChar = Math.max(0.3, (TYPE_END - START) / BOOT_LINES.length)
    let raf = 0, mount = 0
    const step = (t: number) => {
      if (!mount) mount = t
      const e = t - mount
      setN(Math.max(0, Math.min(BOOT_LINES.length, Math.floor((e - START) / perChar))))
      if (box.current) {
        const over = e - TYPE_END
        box.current.style.opacity = over > 0 ? String(Math.max(0, 1 - over / FADE_OUT)) : '1'
      }
      if (e < TYPE_END + FADE_OUT) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [])
  return (
    <div className="crt-boot" aria-hidden="true" ref={box}>
      <pre>{BOOT_LINES.slice(0, n)}<span className="crt-cursor">&#9611;</span></pre>
    </div>
  )
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
          {desktop && <BootLog />}
        </>
      )}
    </>
  )
}
