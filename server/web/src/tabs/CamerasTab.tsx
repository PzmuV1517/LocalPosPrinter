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
  const [loaded, setLoaded] = useState(false)
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

  const { device, cam, online } = feed
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="cam-view" onClick={(e) => e.stopPropagation()}>
        <div className="cam-stage">
          {visible && online
            ? <img src={api.cameraStreamUrl(device.id, cam.node)} alt={cam.name}
                onLoad={() => setLoaded(true)} onError={() => setLoaded(false)} />
            : <div className="cam-placeholder">{online ? 'paused' : 'device offline'}</div>}
          {visible && online && !loaded && <div className="cam-placeholder cam-abs">connecting…</div>}
          {overlay && (
            <div className="cam-overlay">
              <div><b>{cam.name}</b></div>
              <div className="muted">{device.name || device.id} · {cam.node}</div>
              <div className="muted">{online ? 'live' : 'offline'} · {new Date(now).toLocaleString()}</div>
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
                <div className="cam-thumb"><span className="cam-glyph">▣</span></div>
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
