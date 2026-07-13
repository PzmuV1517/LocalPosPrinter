import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { useGuard } from '../common'
import type { Severity } from '../types'

const SEVS: Severity[] = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info']

export function SettingsTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [cfg, setCfg] = useState({ print_width: 384, auto_print_min_sev: 'err' as Severity, auto_print_max_per_min: 30, log_retention_days: 30 })
  const [cfgMsg, setCfgMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [user, setUser] = useState('')
  const [pw, setPw] = useState('')
  const [credMsg, setCredMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [updateLog, setUpdateLog] = useState<string | null>(null)
  const [updating, setUpdating] = useState(false)

  const load = useCallback(async () => {
    const d = await guard(api.getConfig())
    if (d) {
      setCfg({ print_width: d.print_width, auto_print_min_sev: d.auto_print_min_sev, auto_print_max_per_min: d.auto_print_max_per_min, log_retention_days: d.log_retention_days })
      setUser(d.username)
    }
  }, [guard])
  useEffect(() => { load() }, [load])

  async function saveConfig() {
    const res = await guard(api.setConfig(cfg))
    setCfgMsg(res?.ok ? { ok: true, text: 'Saved.' } : { ok: false, text: 'Failed.' })
  }
  async function saveCreds() {
    if (!user.trim()) return setCredMsg({ ok: false, text: 'Username required.' })
    if (pw && pw.length < 4) return setCredMsg({ ok: false, text: 'Password min 4 characters.' })
    const body: Record<string, unknown> = { new_master_username: user.trim() }
    if (pw) body.new_master_password = pw
    const res = await guard(api.setConfig(body))
    if (res?.ok) { setCredMsg({ ok: true, text: 'Credentials updated.' }); setPw('') }
    else { const e = await res?.json().catch(() => ({})); setCredMsg({ ok: false, text: e?.error || 'Failed.' }) }
  }

  async function waitForRestart() {
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 1000))
      if (await api.serverUp()) { setUpdateLog((l) => (l ?? '') + '\nServer back up — reloading…'); setTimeout(() => location.reload(), 900); return }
    }
    setUpdateLog((l) => (l ?? '') + '\nStill waiting — reload manually once it’s back.')
  }
  async function doUpdate() {
    if (!confirm('Pull the latest code from main and restart the server now?')) return
    setUpdating(true); setUpdateLog('Updating…')
    try {
      const d = await guard(api.updateServer())
      if (!d) { setUpdating(false); return }
      const tail = !d.ok ? '✗ Update failed — see log above.'
        : d.changed ? `✓ Updated ${d.before} → ${d.after}. Restarting…` : '✓ Already up to date.'
      setUpdateLog((d.log || '').trim() + '\n\n' + tail)
      if (d.restarting) waitForRestart()
    } catch {
      setUpdateLog((l) => (l ?? '') + '\n\nServer restarting…'); waitForRestart()
    }
    setUpdating(false)
  }
  async function doRestart() {
    if (!confirm('Restart the server now (no code pull)?')) return
    setUpdating(true); setUpdateLog('Restarting…')
    try { await guard(api.restartServer()) } catch { /* connection may drop as it restarts */ }
    waitForRestart()
    setUpdating(false)
  }

  return (
    <>
      <div className="card">
        <h2>Configuration</h2>
        <div className="row">
          <div><label>Print width (px)</label><input type="number" value={cfg.print_width} onChange={(e) => setCfg({ ...cfg, print_width: +e.target.value })} /></div>
          <div><label>Auto-print at</label>
            <select value={cfg.auto_print_min_sev} onChange={(e) => setCfg({ ...cfg, auto_print_min_sev: e.target.value as Severity })}>
              {SEVS.map((s) => <option key={s} value={s}>{s}+</option>)}
            </select>
          </div>
        </div>
        <div className="row">
          <div><label>Auto-print fuse (per min, 0=∞)</label><input type="number" value={cfg.auto_print_max_per_min} onChange={(e) => setCfg({ ...cfg, auto_print_max_per_min: +e.target.value })} /></div>
          <div><label>Log retention (days)</label><input type="number" value={cfg.log_retention_days} onChange={(e) => setCfg({ ...cfg, log_retention_days: +e.target.value })} /></div>
        </div>
        <div style={{ marginTop: 14 }}><button onClick={saveConfig}>Save configuration</button></div>
        {cfgMsg && <div className={`result ${cfgMsg.ok ? 'ok' : 'bad'}`}>{cfgMsg.text}</div>}
      </div>

      <div className="card">
        <h2>Change credentials</h2>
        <div className="row" style={{ alignItems: 'flex-end' }}>
          <div><label>Username</label><input value={user} autoComplete="username" onChange={(e) => setUser(e.target.value)} /></div>
          <div><label>New password (blank = keep)</label><input type="password" value={pw} placeholder="min 4 chars" autoComplete="new-password" onChange={(e) => setPw(e.target.value)} /></div>
          <button style={{ flex: '0 0 auto' }} onClick={saveCreds}>Update</button>
        </div>
        {credMsg && <div className={`result ${credMsg.ok ? 'ok' : 'bad'}`}>{credMsg.text}</div>}
      </div>

      <div className="card">
        <h2>Server updates</h2>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
          Pull the latest code from <span className="mono">main</span> and restart the service — no manual git pull.
          (The service must run under a supervisor that restarts it: systemd <span className="mono">Restart=always</span> or Docker <span className="mono">restart: unless-stopped</span>.)
        </p>
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <button style={{ flex: '0 0 auto' }} onClick={doUpdate} disabled={updating}>Pull latest &amp; restart</button>
          <button className="ghost" style={{ flex: '0 0 auto' }} onClick={doRestart} disabled={updating}>Restart service</button>
        </div>
        {updateLog !== null && <pre className="updatelog mono">{updateLog}</pre>}
      </div>
    </>
  )
}
