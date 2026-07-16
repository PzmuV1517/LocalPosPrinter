import { useState } from 'react'
import * as api from '../api'
import type { Severity } from '../types'

const SEVS: Severity[] = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info']

export function Setup({ onAuthed }: { onAuthed: (token: string) => void }) {
  const [username, setUsername] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [width, setWidth] = useState(384)
  const [sev, setSev] = useState<Severity>('err')
  const [fuse, setFuse] = useState(30)
  const [retention, setRetention] = useState(30)
  const [err, setErr] = useState('')

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setErr('')
    if (!username.trim()) return setErr('Username is required')
    if (pw.length < 4) return setErr('Password must be at least 4 characters')
    if (pw !== pw2) return setErr('Passwords do not match')
    try {
      const token = await api.runSetup({
        username: username.trim(), master_password: pw, print_width: width,
        auto_print_min_sev: sev, auto_print_max_per_min: fuse, log_retention_days: retention,
      })
      onAuthed(token)
    } catch (e) { setErr(String((e as Error).message)) }
  }

  return (
    <div className="center">
      <div className="brand">WATCHTOWER</div>
      <div className="tag">first-run setup</div>
      <form className="panel" onSubmit={submit} autoComplete="on">
        <p className="stepnote">No configuration found. Set this up once, it's saved to the server's database and survives pulls &amp; updates.</p>
        <label>Username</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} />
        <label>Password</label>
        <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" />
        <label>Confirm password</label>
        <input type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} autoComplete="new-password" />
        <div className="row">
          <div><label>Print width (px)</label><input type="number" value={width} onChange={(e) => setWidth(+e.target.value)} /></div>
          <div><label>Auto-print at</label>
            <select value={sev} onChange={(e) => setSev(e.target.value as Severity)}>
              {SEVS.map((s) => <option key={s} value={s}>{s}+</option>)}
            </select>
          </div>
        </div>
        <div className="row">
          <div><label>Auto-print fuse (per min, 0=∞)</label><input type="number" value={fuse} onChange={(e) => setFuse(+e.target.value)} /></div>
          <div><label>Log retention (days)</label><input type="number" value={retention} onChange={(e) => setRetention(+e.target.value)} /></div>
        </div>
        <div style={{ marginTop: 16 }}><button type="submit" style={{ width: '100%' }}>Complete setup</button></div>
        <div className="err">{err}</div>
      </form>
    </div>
  )
}
