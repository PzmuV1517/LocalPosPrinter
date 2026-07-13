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

export function DeviceCard(
  { d, counts, actions }: {
    d: Device; counts: SevCounts;
    actions?: { onRotate: () => void; onRevoke: () => void; onDelete: () => void }
  },
) {
  const online = !!d.last_seen_at && Date.now() / 1000 - d.last_seen_at < 120
  return (
    <div className="device">
      <div className="name">{d.name || d.id} {d.revoked && <span className="pill bad">revoked</span>}</div>
      <div className="id mono">{d.id}</div>
      <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        <span className={`dot ${online ? 'on' : ''}`} />{online ? 'online' : 'offline'} · {d.last_seen_at ? fmtTime(d.last_seen_at) : 'never'}
      </div>
      <div className="sevs"><SevPills sevs={counts[d.id] || {}} /></div>
      {actions && (
        <div className="actions">
          <button className="ghost mini" onClick={actions.onRotate}>{d.revoked ? 'Reactivate' : 'Rotate'}</button>
          {d.revoked
            ? <button className="ghost mini" onClick={actions.onDelete}>Delete</button>
            : <button className="ghost mini" onClick={actions.onRevoke}>Revoke</button>}
        </div>
      )}
    </div>
  )
}
