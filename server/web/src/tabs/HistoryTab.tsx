import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { fmtTime, useGuard } from '../common'
import type { HistoryRow } from '../types'

export function HistoryTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [rows, setRows] = useState<HistoryRow[]>([])

  const load = useCallback(async () => {
    const d = await guard(api.adminState())
    if (d) setRows(d.history)
  }, [guard])
  useEffect(() => { load() }, [load])

  return (
    <div className="card">
      <h2>Print history</h2>
      <div className="scroll">
        <table>
          <thead><tr><th>Time</th><th>Format</th><th>User</th><th>Label</th><th>Status</th></tr></thead>
          <tbody>
            {rows.length === 0
              ? <tr><td colSpan={5} className="muted">Empty.</td></tr>
              : rows.map((h, i) => (
                <tr key={i}>
                  <td className="muted" style={{ whiteSpace: 'nowrap' }}>{fmtTime(h.timestamp)}</td>
                  <td>{h.format}</td><td>{h.user}</td><td className="msg">{h.label}</td><td>{h.status}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
