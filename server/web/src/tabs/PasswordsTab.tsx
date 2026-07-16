import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { useGuard } from '../common'
import type { TempPassword } from '../types'

export function PasswordsTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [rows, setRows] = useState<TempPassword[]>([])
  const [user, setUser] = useState('')
  const [maxUses, setMaxUses] = useState(1)
  const [pw, setPw] = useState('')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const load = useCallback(async () => {
    const d = await guard(api.adminState())
    if (d) setRows(d.passwords)
  }, [guard])
  useEffect(() => { load() }, [load])

  async function create() {
    const res = await guard(api.createPassword(user.trim(), maxUses || 1, pw.trim()))
    if (!res) return
    const d = await res.json()
    if (!res.ok) { setMsg({ ok: false, text: d.error || 'failed' }); return }
    setMsg({ ok: true, text: `Created: ${d.password.password}  (${d.password.remaining} uses)` })
    setUser(''); setPw(''); load()
  }

  return (
    <>
      <div className="card">
        <h2>Create limited-use password</h2>
        <div className="row" style={{ alignItems: 'flex-end' }}>
          <div><label>User label</label><input value={user} placeholder="who it's for" onChange={(e) => setUser(e.target.value)} /></div>
          <div><label>Max uses</label><input type="number" min={1} value={maxUses} onChange={(e) => setMaxUses(+e.target.value)} /></div>
          <div><label>Password (blank = random)</label><input value={pw} placeholder="optional" onChange={(e) => setPw(e.target.value)} /></div>
          <button style={{ flex: '0 0 auto' }} onClick={create}>Create</button>
        </div>
        {msg && <div className={`result ${msg.ok ? 'ok' : 'bad'}`}>{msg.text}</div>}
      </div>
      <div className="card">
        <h2>Active passwords</h2>
        <div className="scroll">
          <table>
            <thead><tr><th>User</th><th>Used / Max</th><th>Remaining</th><th>Status</th></tr></thead>
            <tbody>
              {rows.length === 0
                ? <tr><td colSpan={4} className="muted">None.</td></tr>
                : rows.map((p, i) => (
                  <tr key={i}>
                    <td>{p.user || '-'}</td><td>{p.used} / {p.max_uses}</td><td>{p.remaining}</td>
                    <td><span className={`pill ${p.active ? 'ok' : 'bad'}`}>{p.active ? 'active' : (p.revoked ? 'revoked' : 'used up')}</span></td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
