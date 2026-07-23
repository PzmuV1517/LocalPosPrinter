import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { DeviceCard, fmtTime, useGuard, useInterval, SEV_ORDER } from '../common'
import type { LogsResponse, LogRow, Severity } from '../types'

const POLL_MS = 2000 // faster than before (was 5000)

type Series = { start: number; width: number; buckets: number; err: number[]; other: number[] }

function ErrorRateChart({ s }: { s: Series }) {
  const max = Math.max(1, ...s.err.map((e, i) => e + s.other[i]))
  const fmt = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return (
    <>
      <div className="chart">
        {s.err.map((e, i) => {
          const total = e + s.other[i]
          return <div key={i} className={`bar ${e > 0 ? 'err' : ''}`} style={{ height: `${(total / max) * 100}%` }}
            title={`${fmt(s.start + i * s.width)}, ${e} err, ${s.other[i]} other`} />
        })}
      </div>
      <div className="chart-axis"><span>{fmt(s.start)}</span><span>24h, errors highlighted</span><span>now</span></div>
    </>
  )
}

function LogModal({ log, onClose, onLower }: {
  log: LogRow; onClose: () => void; onLower: (target: string) => void
}) {
  const lower = SEV_ORDER.slice(SEV_ORDER.indexOf(log.severity) + 1)
  const [target, setTarget] = useState<string>(lower[0] || 'info')
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <span className={`pill ${log.sev_num <= 3 ? 'bad' : 'ok'}`}>{log.severity}</span>
          <button className="ghost mini" onClick={onClose}>close</button>
        </div>
        <div className="mono" style={{ fontSize: 12, lineHeight: 1.7 }}>
          <div><span className="muted">time&nbsp;&nbsp;&nbsp;</span>{fmtTime(log.ts)}</div>
          <div><span className="muted">device&nbsp;</span>{log.device_id}</div>
          <div><span className="muted">service</span> {log.service || '-'}</div>
          <div><span className="muted">source&nbsp;</span>{log.source_ip || '-'} · printed={String(log.printed)}</div>
        </div>
        <div style={{ marginTop: 12 }} className="muted">message</div>
        <pre>{log.message}</pre>
        {Object.keys(log.meta || {}).length > 0 && <>
          <div className="muted" style={{ marginTop: 10 }}>meta</div>
          <pre>{JSON.stringify(log.meta, null, 2)}</pre>
        </>}
        {lower.length > 0 && (
          <div style={{ marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
            <div className="muted">lower severity of messages like this</div>
            <div className="row" style={{ alignItems: 'flex-end', marginTop: 6 }}>
              <div style={{ flex: '0 0 auto' }}>
                <select value={target} onChange={(e) => setTarget(e.target.value)}>
                  {lower.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <button className="ghost mini" style={{ flex: '0 0 auto' }} onClick={() => onLower(target)}>Apply</button>
            </div>
            <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
              future logs from "{log.service || 'any'}" containing this message get severity {target}. Manage in Settings.
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export function LogsTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [data, setData] = useState<LogsResponse | null>(null)
  const [series, setSeries] = useState<Series | null>(null)
  const [auto, setAuto] = useState(true)
  const [stamp, setStamp] = useState('')
  const [f, setF] = useState({ max_sev: '', device_id: '', service: '', search: '' })
  const [selected, setSelected] = useState<LogRow | null>(null)

  const filters = useCallback(() => {
    const body: Record<string, unknown> = {}
    if (f.max_sev) body.max_sev = f.max_sev
    if (f.device_id.trim()) body.device_id = f.device_id.trim()
    if (f.service.trim()) body.service = f.service.trim()
    if (f.search.trim()) body.search = f.search.trim()
    return body
  }, [f])

  const load = useCallback(async () => {
    const res = await guard(api.getLogs({ ...filters(), limit: 200 }))
    if (res) { setData(res); setStamp(new Date().toLocaleTimeString()) }
  }, [filters, guard])

  const loadChart = useCallback(async () => {
    const s = await guard(api.metricsSeries(24))
    if (s) setSeries(s)
  }, [guard])

  useEffect(() => { load() }, [load])
  useEffect(() => { loadChart() }, [loadChart])
  useInterval(load, POLL_MS, auto)
  useInterval(loadChart, 30000, true)

  async function print(id: number) { await guard(api.printLog(id)); load() }
  async function lower(log: LogRow, target: string) {
    await guard(api.addOverride(log.service, log.message, target))
    setSelected(null); load()
  }

  const logs = data?.logs ?? []
  return (
    <>
      <div className="card">
        <h2>Devices</h2>
        <div className="cards">
          {data && data.devices.length
            ? data.devices.map((d) => <DeviceCard key={d.id} d={d} counts={data.counts} hostErrors={data.host_errors} />)
            : <div className="muted" style={{ fontSize: 12 }}>No devices yet, issue a secret in the Devices tab.</div>}
        </div>
      </div>

      <div className="card">
        <h2>Error rate (24h)</h2>
        {series ? <ErrorRateChart s={series} /> : <div className="muted" style={{ fontSize: 12 }}>…</div>}
      </div>

      <div className="card">
        <h2>Log stream</h2>
        <div className="filters">
          <div><label>Severity ≤</label>
            <select value={f.max_sev} onChange={(e) => setF({ ...f, max_sev: e.target.value })}>
              <option value="">all</option><option value="err">err+</option><option value="crit">crit+</option>
              <option value="warning">warning+</option><option value="info">info+</option>
            </select>
          </div>
          <div><label>Device</label><input value={f.device_id} placeholder="any" onChange={(e) => setF({ ...f, device_id: e.target.value })} /></div>
          <div><label>Service</label><input value={f.service} placeholder="any" onChange={(e) => setF({ ...f, service: e.target.value })} /></div>
          <div><label>Search</label><input value={f.search} placeholder="text…" onChange={(e) => setF({ ...f, search: e.target.value })} /></div>
          <button className="ghost mini" onClick={load}>Refresh</button>
          <button className="ghost mini" onClick={() => api.exportLogsCsv(filters())}>Export CSV</button>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, margin: 0, textTransform: 'none' }}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} style={{ width: 'auto' }} /> auto
          </label>
        </div>
        <div className="scroll">
          <table>
            <thead><tr><th>Time</th><th>Sev</th><th>Device</th><th>Service</th><th>Message</th><th /></tr></thead>
            <tbody>
              {logs.length === 0
                ? <tr><td colSpan={6} className="muted">No logs match.</td></tr>
                : logs.map((l: LogRow) => (
                  <tr key={l.id} className="clickable" onClick={() => setSelected(l)}>
                    <td className="muted" style={{ whiteSpace: 'nowrap' }}>{fmtTime(l.ts)}</td>
                    <td><span className={`pill ${l.sev_num <= 3 ? 'bad' : 'ok'}`}>{l.severity as Severity}</span></td>
                    <td className="mono">{l.device_id}</td>
                    <td>{l.service}</td>
                    <td className="msg">{l.message}{l.printed && <span className="pill ok"> printed</span>}</td>
                    <td><button className="ghost mini" onClick={(e) => { e.stopPropagation(); print(l.id) }}>Print</button></td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        <div className="muted" style={{ marginTop: 8, fontSize: 11 }}>{logs.length} shown{stamp && ` · ${stamp}`} · click a row for detail</div>
      </div>

      {selected && <LogModal log={selected} onClose={() => setSelected(null)} onLower={(t) => lower(selected, t)} />}
    </>
  )
}
