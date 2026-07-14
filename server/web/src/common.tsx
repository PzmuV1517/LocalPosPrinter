import { useCallback, useEffect, useRef, useState } from 'react'
import { Unauthorized } from './api'
import type { Device, SevCounts, Severity } from './types'

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

export function DeviceCard(
  { d, counts, actions }: { d: Device; counts: SevCounts; actions?: DeviceActions },
) {
  const online = d.agent_online || (!!d.last_seen_at && Date.now() / 1000 - d.last_seen_at < 120)
  const version = (d.meta?.scout_version as string) || ''
  const [hb, setHb] = useState(String(d.heartbeat_secs || 0))
  const [cmd, setCmd] = useState('')
  const metrics = (d.meta?.metrics as Record<string, unknown>) || {}
  return (
    <div className="device">
      <div className="name">{d.name || d.id} {d.revoked && <span className="pill bad">revoked</span>}</div>
      <div className="id mono">{d.id}</div>
      <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        <span className={`dot ${online ? 'on' : ''}`} />
        {d.agent_online ? 'agent online' : online ? 'online' : 'offline'} · {d.last_seen_at ? fmtTime(d.last_seen_at) : 'never'}
        {version && <> · scout {version}</>}
      </div>
      <HostMetrics m={metrics} />
      <div className="sevs"><SevPills sevs={counts[d.id] || {}} /></div>
      {actions && !d.revoked && <>
        <div className="devctl">
          <span className="muted" style={{ fontSize: 11 }}>heartbeat</span>
          <input value={hb} onChange={(e) => setHb(e.target.value)} style={{ width: 60 }} />
          <span className="muted" style={{ fontSize: 11 }}>s</span>
          <button className="ghost mini" onClick={() => actions.onSetHeartbeat(parseInt(hb, 10) || 0)}>set</button>
        </div>
        <div className="devctl">
          <input value={cmd} placeholder="shell command…" onChange={(e) => setCmd(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && cmd.trim()) { actions.onRun(cmd.trim()); setCmd('') } }} />
          <button className="ghost mini" onClick={() => { if (cmd.trim()) { actions.onRun(cmd.trim()); setCmd('') } }}>run</button>
        </div>
      </>}
      {actions && (
        <div className="actions" style={{ flexWrap: 'wrap' }}>
          {!d.revoked && <>
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
