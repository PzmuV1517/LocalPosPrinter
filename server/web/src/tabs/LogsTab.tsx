import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { DeviceCard, fmtTime, useGuard, useInterval } from '../common'
import type { LogsResponse, LogRow, Severity } from '../types'

const POLL_MS = 2000 // faster than before (was 5000)

export function LogsTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [data, setData] = useState<LogsResponse | null>(null)
  const [auto, setAuto] = useState(true)
  const [stamp, setStamp] = useState('')
  const [f, setF] = useState({ max_sev: '', device_id: '', service: '', search: '' })

  const load = useCallback(async () => {
    const body: Record<string, unknown> = { limit: 200 }
    if (f.max_sev) body.max_sev = f.max_sev
    if (f.device_id.trim()) body.device_id = f.device_id.trim()
    if (f.service.trim()) body.service = f.service.trim()
    if (f.search.trim()) body.search = f.search.trim()
    const res = await guard(api.getLogs(body))
    if (res) { setData(res); setStamp(new Date().toLocaleTimeString()) }
  }, [f, guard])

  useEffect(() => { load() }, [load])
  useInterval(load, POLL_MS, auto)

  async function print(id: number) {
    await guard(api.printLog(id))
    load()
  }

  const logs = data?.logs ?? []
  return (
    <>
      <div className="card">
        <h2>Devices</h2>
        <div className="cards">
          {data && data.devices.length
            ? data.devices.map((d) => <DeviceCard key={d.id} d={d} counts={data.counts} />)
            : <div className="muted" style={{ fontSize: 12 }}>No devices yet — issue a secret in the Devices tab.</div>}
        </div>
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
                  <tr key={l.id}>
                    <td className="muted" style={{ whiteSpace: 'nowrap' }}>{fmtTime(l.ts)}</td>
                    <td><span className={`pill ${l.sev_num <= 3 ? 'bad' : 'ok'}`}>{l.severity as Severity}</span></td>
                    <td className="mono">{l.device_id}</td>
                    <td>{l.service}</td>
                    <td className="msg">{l.message}{l.printed && <span className="pill ok"> printed</span>}</td>
                    <td><button className="ghost mini" onClick={() => print(l.id)}>Print</button></td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        <div className="muted" style={{ marginTop: 8, fontSize: 11 }}>{logs.length} shown{stamp && ` · ${stamp}`}</div>
      </div>
    </>
  )
}
