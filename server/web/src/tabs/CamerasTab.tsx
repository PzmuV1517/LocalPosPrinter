import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { useGuard, useInterval, fmtTime } from '../common'
import type { Camera, Device } from '../types'

interface Feed { device: Device; cam: Camera; online: boolean }

/** The selected cameras across all devices, flattened for the grid. A node the operator picked but
 *  whose device is offline still shows (as offline) so it doesn't silently vanish. */
function feeds(devices: Device[]): Feed[] {
  const out: Feed[] = []
  for (const d of devices) {
    const online = !!d.agent_online
    const cams = (d.meta?.cameras as Camera[]) || []
    const sel = (d.meta?.cameras_selected as string[]) || []
    for (const node of sel) {
      const cam = cams.find((c) => c.node === node) || { node, name: node }
      out.push({ device: d, cam, online })
    }
  }
  return out
}

/** Live feed with a toggleable overlay. Mounted only while focused, so exactly one stream runs;
 *  closing it (or leaving the tab) unmounts the <img>, which drops the connection and stops the
 *  scout's camera. Also unmounts while the browser tab is hidden. */
function FocusedCamera({ feed, onClose }: { feed: Feed; onClose: () => void }) {
  const [overlay, setOverlay] = useState(true)
  const [state, setState] = useState<'connecting' | 'live' | 'error'>('connecting')
  const [slow, setSlow] = useState(false)
  const [gen, setGen] = useState(0)  // bump to remount the <img> and reconnect
  const [now, setNow] = useState(Date.now())
  const [visible, setVisible] = useState(document.visibilityState === 'visible')
  useEffect(() => {
    const h = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', h)
    return () => document.removeEventListener('visibilitychange', h)
  }, [])
  useInterval(() => setNow(Date.now()), 1000)
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  // If no frame lands in a few seconds, say so (scout may need an update / ffmpeg / proxy tweak).
  useEffect(() => {
    if (state !== 'connecting') return
    setSlow(false)
    const t = setTimeout(() => setSlow(true), 6000)
    return () => clearTimeout(t)
  }, [state, gen])
  const retry = () => { setState('connecting'); setGen((g) => g + 1) }

  const { device, cam, online } = feed
  const showImg = visible && online && state !== 'error'
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="cam-view" onClick={(e) => e.stopPropagation()}>
        <div className="cam-stage">
          {showImg && (
            <img key={gen} src={api.cameraStreamUrl(device.id, cam.node)} alt={cam.name}
              onLoad={() => setState('live')} onError={() => setState('error')} />
          )}
          {!online && <div className="cam-placeholder">device offline</div>}
          {online && !visible && <div className="cam-placeholder">paused</div>}
          {showImg && state === 'connecting' && (
            <div className="cam-placeholder cam-abs">
              connecting…
              {slow && <div className="muted" style={{ marginTop: 6, maxWidth: 320 }}>
                No frames yet. Check the scout is updated to 2.3.0 and has ffmpeg installed.
              </div>}
            </div>
          )}
          {online && visible && state === 'error' && (
            <div className="cam-placeholder cam-abs">
              no signal
              <div className="muted" style={{ marginTop: 6, maxWidth: 320 }}>
                Scout is not sending frames. Update it to 2.3.0, install ffmpeg, and (behind a proxy)
                disable request buffering for /agent/camera/push.
              </div>
              <button className="ghost mini" style={{ marginTop: 10 }} onClick={retry}>Retry</button>
            </div>
          )}
          {overlay && (
            <div className="cam-overlay">
              <div><b>{cam.name}</b></div>
              <div className="muted">{device.name || device.id} · {cam.node}</div>
              <div className="muted">{online ? state : 'offline'} · {new Date(now).toLocaleString()}</div>
            </div>
          )}
        </div>
        <div className="cam-bar">
          <button className="ghost mini" onClick={() => setOverlay((o) => !o)}>{overlay ? 'Hide info' : 'Show info'}</button>
          <button className="ghost mini" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  )
}

/** Still thumbnail for a grid tile: one cached/briefly-captured frame, glyph if offline or none. */
function Thumb({ feed }: { feed: Feed }) {
  const [failed, setFailed] = useState(false)
  if (!feed.online || failed) return <div className="cam-thumb"><span className="cam-glyph">▣</span></div>
  return (
    <div className="cam-thumb">
      <img src={api.cameraSnapshotUrl(feed.device.id, feed.cam.node)} alt={feed.cam.name}
        loading="lazy" onError={() => setFailed(true)} />
    </div>
  )
}

export function CamerasTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [devices, setDevices] = useState<Device[]>([])
  const [focus, setFocus] = useState<Feed | null>(null)

  const load = useCallback(async () => {
    const res = await guard(api.getLogs({ limit: 1 }))
    if (res) setDevices(res.devices)
  }, [guard])
  useEffect(() => { load() }, [load])
  useInterval(load, 5000, !focus)  // refresh presence, but not while watching a feed

  const list = feeds(devices)

  return (
    <div className="card">
      <h2>Cameras</h2>
      {list.length === 0
        ? <div className="muted" style={{ fontSize: 12 }}>
            No cameras selected. Pick cameras on a device in the Devices tab (they appear once a
            scout with a webcam is online).
          </div>
        : <div className="cam-grid">
            {list.map((f) => (
              <button key={`${f.device.id}|${f.cam.node}`} className="cam-tile" onClick={() => setFocus(f)}
                disabled={!f.online} title={f.online ? 'Click to view' : 'Device offline'}>
                <Thumb feed={f} />
                <div className="cam-meta">
                  <div className="cam-name">{f.cam.name}</div>
                  <div className="muted">
                    <span className={`dot ${f.online ? 'on' : 'off'}`} />
                    {f.device.name || f.device.id}
                  </div>
                  <div className="muted" style={{ fontSize: 10 }}>
                    {f.online ? 'online' : `offline · ${fmtTime(f.device.last_seen_at)}`}
                  </div>
                </div>
              </button>
            ))}
          </div>}
      {focus && <FocusedCamera feed={focus} onClose={() => setFocus(null)} />}
    </div>
  )
}
