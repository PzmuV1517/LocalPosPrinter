import { useCallback, useEffect, useRef, useState } from 'react'
import { Unauthorized } from './api'
import type { Camera, Device, Guest, SevCounts, Severity } from './types'

/** Wrap API calls so a 401 (expired/invalid token) bounces to the login gate. */
export function useGuard(onUnauthorized: () => void) {
  return useCallback(
    async <T,>(p: Promise<T>): Promise<T | undefined> => {
      try { return await p } catch (e) {
        if (e instanceof Unauthorized) { onUnauthorized(); return undefined }
        throw e
      }
    },
    [onUnauthorized],
  )
}

export const SEV_ORDER: Severity[] = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug']
export const BAD = new Set<Severity>(['emerg', 'alert', 'crit', 'err'])

export const fmtTime = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : '')

/** setInterval as a hook that pauses when `active` is false or the tab is hidden. */
export function useInterval(cb: () => void, ms: number, active = true) {
  const saved = useRef(cb)
  saved.current = cb
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') saved.current()
    }, ms)
    return () => clearInterval(id)
  }, [ms, active])
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      className="ghost mini"
      onClick={() => {
        navigator.clipboard.writeText(text).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1000)
        })
      }}
    >
      {done ? 'Copied' : label}
    </button>
  )
}

export function SevPills({ sevs }: { sevs: Partial<Record<Severity, number>> }) {
  const shown = SEV_ORDER.filter((s) => sevs[s])
  if (!shown.length) return <span className="muted" style={{ fontSize: 11 }}>no logs 24h</span>
  return (
    <>
      {shown.map((s) => (
        <span key={s} className={`pill ${BAD.has(s) ? 'bad' : 'ok'}`}>{s} {sevs[s]}</span>
      ))}
    </>
  )
}

interface DeviceActions {
  onRotate: () => void; onRevoke: () => void; onDelete: () => void
  onUpdate: () => void; onPing: () => void; onRestart: () => void
  onSetHeartbeat: (secs: number) => void; onRun: (cmd: string) => void
  onSelectCamera: (node: string, selected: boolean) => void
}

function HostMetrics({ m }: { m: Record<string, unknown> }) {
  const num = (k: string) => (typeof m[k] === 'number' ? (m[k] as number) : undefined)
  const disk = num('disk_pct'), mem = num('mem_pct'), load = num('load1'), temp = num('temp_c')
  if (disk === undefined && mem === undefined && load === undefined) return null
  return (
    <div className="metrics">
      {disk !== undefined && <span className={disk >= 90 ? 'warn' : ''}>disk <b>{disk}%</b></span>}
      {mem !== undefined && <span>mem <b>{mem}%</b></span>}
      {load !== undefined && <span>load <b>{load}</b></span>}
      {temp !== undefined && <span>temp <b>{temp}°C</b></span>}
    </div>
  )
}

function ProxmoxGuests({ guests }: { guests: Guest[] }) {
  const up = guests.filter((g) => g.status === 'running').length
  return (
    <div className="dev-section">
      <div className="dev-label">proxmox guests · {up}/{guests.length} up</div>
      <div className="guests">
        {guests.map((g) => (
          <span key={`${g.kind}${g.vmid}`} className="guest" title={`${g.kind} ${g.vmid} · ${g.status}`}>
            <span className={`dot ${g.status === 'running' ? 'on' : 'off'}`} />
            {g.name}<span className="muted"> {g.kind}{g.vmid}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

export function DeviceCard(
  { d, counts, actions }: { d: Device; counts: SevCounts; actions?: DeviceActions },
) {
  const online = d.agent_online || (!!d.last_seen_at && Date.now() / 1000 - d.last_seen_at < 120)
  const isPrinter = d.meta?.role === 'printer'
  const version = (d.meta?.scout_version as string) || ''
  const battery = d.meta?.battery as number | undefined
  const serial = d.meta?.serial as string | undefined
  const [hb, setHb] = useState(String(d.heartbeat_secs || 0))
  const [cmd, setCmd] = useState('')
  const metrics = (d.meta?.metrics as Record<string, unknown>) || {}
  const cameras = (d.meta?.cameras as Camera[]) || []
  const selected = (d.meta?.cameras_selected as string[]) || []
  const guests = (d.meta?.proxmox as { guests?: Guest[] } | undefined)?.guests
  const ctl = actions && !d.revoked && !isPrinter
  return (
    <div className="device">
      <div className="dev-head">
        <div className="name">{d.name || d.id}</div>
        <span className="pill">{isPrinter ? 'printer' : 'scout'}</span>
        {d.revoked && <span className="pill bad">revoked</span>}
      </div>
      <div className="id mono">{d.id}</div>
      <div className="muted dev-status">
        <span className={`dot ${online ? 'on' : 'off'}`} />
        {d.agent_online ? 'agent online' : online ? 'online' : 'offline'} · {d.last_seen_at ? fmtTime(d.last_seen_at) : 'never'}
        {isPrinter
          ? (battery != null && <> · {battery}% {d.meta?.charging ? 'charging' : 'on battery'}</>)
          : (version && <> · scout {version}</>)}
      </div>
      {isPrinter ? (serial && <div className="muted" style={{ fontSize: 11 }}>serial {serial}</div>)
        : <HostMetrics m={metrics} />}
      <div className="sevs"><SevPills sevs={counts[d.id] || {}} /></div>

      {guests && guests.length > 0 && <ProxmoxGuests guests={guests} />}

      {ctl && cameras.length > 0 && (
        <div className="dev-section">
          <div className="dev-label">cameras</div>
          {cameras.map((c) => (
            <label className="cam-pick" key={c.node}>
              <input type="checkbox" checked={selected.includes(c.node)}
                onChange={(e) => actions!.onSelectCamera(c.node, e.target.checked)} />
              <span className="cam-pick-name">{c.name}</span>
              <span className="muted mono">{c.node}</span>
            </label>
          ))}
        </div>
      )}

      {ctl && (
        <div className="dev-section">
          <div className="dev-label">controls</div>
          <div className="dev-line">
            <span className="muted">heartbeat</span>
            <input value={hb} onChange={(e) => setHb(e.target.value)} style={{ width: 64 }} />
            <span className="muted">s</span>
            <button className="ghost mini" onClick={() => actions!.onSetHeartbeat(parseInt(hb, 10) || 0)}>set</button>
          </div>
          <div className="dev-line">
            <input value={cmd} placeholder="shell command…" onChange={(e) => setCmd(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && cmd.trim()) { actions!.onRun(cmd.trim()); setCmd('') } }} />
            <button className="ghost mini" onClick={() => { if (cmd.trim()) { actions!.onRun(cmd.trim()); setCmd('') } }}>run</button>
          </div>
        </div>
      )}

      {actions && (
        <div className="dev-btns">
          {ctl && <>
            <button className="ghost mini" onClick={actions.onPing}>Ping</button>
            <button className="ghost mini" onClick={actions.onRestart}>Restart</button>
            <button className="ghost mini" onClick={actions.onUpdate}>Update</button>
          </>}
          <button className="ghost mini" onClick={actions.onRotate}>{d.revoked ? 'Reactivate' : 'Rotate'}</button>
          {d.revoked
            ? <button className="ghost mini" onClick={actions.onDelete}>Delete</button>
            : <button className="ghost mini" onClick={actions.onRevoke}>Revoke</button>}
        </div>
      )}
    </div>
  )
}
