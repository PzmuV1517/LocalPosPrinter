import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { fmtTime, useGuard } from '../common'
import type { NotifySettings, Severity } from '../types'

const SEVS: Severity[] = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info']

const EMPTY_NOTIFY: NotifySettings = {
  enabled: false, host: '', port: 587, security: 'starttls', username: '',
  from_addr: '', to_addr: '', min_sev: 'crit', has_password: false,
}

export function SettingsTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [cfg, setCfg] = useState({
    print_width: 384, auto_print_min_sev: 'err' as Severity, auto_print_max_per_min: 30,
    log_retention_days: 30, err_retention_days: 0, disk_alert_pct: 90,
  })
  const [cfgMsg, setCfgMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [user, setUser] = useState('')
  const [pw, setPw] = useState('')
  const [credMsg, setCredMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [notify, setNotify] = useState<NotifySettings>(EMPTY_NOTIFY)
  const [smtpPw, setSmtpPw] = useState('')
  const [notifyMsg, setNotifyMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [updateLog, setUpdateLog] = useState<string | null>(null)
  const [updating, setUpdating] = useState(false)
  const [passkeys, setPasskeys] = useState<api.Passkey[]>([])
  const [pkMsg, setPkMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const loadPasskeys = useCallback(async () => {
    const d = await guard(api.listPasskeys())
    if (d) setPasskeys(d.passkeys)
  }, [guard])

  const load = useCallback(async () => {
    const d = await guard(api.getConfig())
    if (d) {
      setCfg({
        print_width: d.print_width, auto_print_min_sev: d.auto_print_min_sev,
        auto_print_max_per_min: d.auto_print_max_per_min, log_retention_days: d.log_retention_days,
        err_retention_days: d.err_retention_days, disk_alert_pct: d.disk_alert_pct,
      })
      setUser(d.username)
      setNotify(d.notify || EMPTY_NOTIFY)
    }
    loadPasskeys()
  }, [guard, loadPasskeys])
  useEffect(() => { load() }, [load])

  async function addPasskey() {
    const label = prompt('Name this passkey (e.g. "Mac Touch ID", "Phone"):', 'this device')
    if (label === null) return
    setPkMsg({ ok: true, text: 'Follow your browser/device prompt…' })
    try {
      await api.registerPasskey(label || 'passkey')
      setPkMsg({ ok: true, text: 'Passkey added.' })
      loadPasskeys()
    } catch (e) { setPkMsg({ ok: false, text: String((e as Error).message) }) }
  }
  async function removePasskey(id: string) {
    if (!confirm('Remove this passkey?')) return
    await guard(api.deletePasskey(id)); loadPasskeys()
  }

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
  async function saveNotify() {
    const payload: Record<string, unknown> = {
      enabled: notify.enabled, host: notify.host, port: notify.port, security: notify.security,
      username: notify.username, from_addr: notify.from_addr, to_addr: notify.to_addr, min_sev: notify.min_sev,
    }
    if (smtpPw) payload.password = smtpPw
    const res = await guard(api.setConfig({ notify: payload }))
    if (res?.ok) { setNotifyMsg({ ok: true, text: 'Saved.' }); setSmtpPw(''); load() }
    else setNotifyMsg({ ok: false, text: 'Failed.' })
  }
  async function sendTest() {
    setNotifyMsg({ ok: true, text: 'Sending…' })
    const d = await guard(api.testEmail())
    if (d) setNotifyMsg({ ok: d.ok, text: d.ok ? 'Test email sent.' : `Failed: ${d.message}` })
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
    } catch { setUpdateLog((l) => (l ?? '') + '\n\nServer restarting…'); waitForRestart() }
    setUpdating(false)
  }
  async function doRestart() {
    if (!confirm('Restart the server now (no code pull)?')) return
    setUpdating(true); setUpdateLog('Restarting…')
    try { await guard(api.restartServer()) } catch { /* connection may drop */ }
    waitForRestart(); setUpdating(false)
  }

  const N = (patch: Partial<NotifySettings>) => setNotify({ ...notify, ...patch })

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
          <div><label>Auto-print fuse (/min, 0=∞)</label><input type="number" value={cfg.auto_print_max_per_min} onChange={(e) => setCfg({ ...cfg, auto_print_max_per_min: +e.target.value })} /></div>
        </div>
        <div className="row">
          <div><label>Log retention (days)</label><input type="number" value={cfg.log_retention_days} onChange={(e) => setCfg({ ...cfg, log_retention_days: +e.target.value })} /></div>
          <div><label>Error retention (days, 0=same)</label><input type="number" value={cfg.err_retention_days} onChange={(e) => setCfg({ ...cfg, err_retention_days: +e.target.value })} /></div>
          <div><label>Disk-full alert at % (0=off)</label><input type="number" value={cfg.disk_alert_pct} onChange={(e) => setCfg({ ...cfg, disk_alert_pct: +e.target.value })} /></div>
        </div>
        <div style={{ marginTop: 14 }}><button onClick={saveConfig}>Save configuration</button></div>
        {cfgMsg && <div className={`result ${cfgMsg.ok ? 'ok' : 'bad'}`}>{cfgMsg.text}</div>}
      </div>

      <div className="card">
        <h2>Notifications (email)</h2>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
          Email the operator when a log at the chosen severity or worse arrives (and on silence/disk alerts). Deduped.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, textTransform: 'none' }}>
          <input type="checkbox" checked={notify.enabled} onChange={(e) => N({ enabled: e.target.checked })} style={{ width: 'auto' }} /> Enable email notifications
        </label>
        <div className="row">
          <div style={{ flex: 2 }}><label>SMTP host</label><input value={notify.host} placeholder="mail.andreibanu.com" onChange={(e) => N({ host: e.target.value })} /></div>
          <div><label>Port</label><input type="number" value={notify.port} onChange={(e) => N({ port: +e.target.value })} /></div>
          <div><label>Security</label>
            <select value={notify.security} onChange={(e) => N({ security: e.target.value })}>
              <option value="starttls">STARTTLS</option><option value="ssl">SSL</option><option value="none">none</option>
            </select>
          </div>
        </div>
        <div className="row">
          <div><label>Username</label><input value={notify.username} autoComplete="off" placeholder="watchdog@andreibanu.com" onChange={(e) => N({ username: e.target.value })} /></div>
          <div><label>Password {notify.has_password && '(set — blank keeps)'}</label><input type="password" value={smtpPw} autoComplete="new-password" onChange={(e) => setSmtpPw(e.target.value)} /></div>
        </div>
        <div className="row">
          <div><label>From</label><input value={notify.from_addr} placeholder="watchdog@andreibanu.com" onChange={(e) => N({ from_addr: e.target.value })} /></div>
          <div><label>To</label><input value={notify.to_addr} placeholder="contact@andreibanu.com" onChange={(e) => N({ to_addr: e.target.value })} /></div>
          <div><label>Notify at</label>
            <select value={notify.min_sev} onChange={(e) => N({ min_sev: e.target.value as Severity })}>
              {SEVS.map((s) => <option key={s} value={s}>{s}+</option>)}
            </select>
          </div>
        </div>
        <div className="row" style={{ marginTop: 14 }}>
          <button style={{ flex: '0 0 auto' }} onClick={saveNotify}>Save notifications</button>
          <button className="ghost" style={{ flex: '0 0 auto' }} onClick={sendTest}>Send test email</button>
        </div>
        {notifyMsg && <div className={`result ${notifyMsg.ok ? 'ok' : 'bad'}`}>{notifyMsg.text}</div>}
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
        <h2>Passkeys (fingerprint / Touch ID / Windows Hello)</h2>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
          Add a passkey on each device you use (Mac, laptop, phone) to sign in with your fingerprint. Requires HTTPS.
        </p>
        <div className="scroll">
          <table>
            <thead><tr><th>Name</th><th>Added</th><th>Last used</th><th /></tr></thead>
            <tbody>
              {passkeys.length === 0
                ? <tr><td colSpan={4} className="muted">No passkeys yet.</td></tr>
                : passkeys.map((p) => (
                  <tr key={p.credential_id}>
                    <td>{p.label || 'passkey'}</td>
                    <td className="muted">{fmtTime(p.created_at)}</td>
                    <td className="muted">{p.last_used_at ? fmtTime(p.last_used_at) : 'never'}</td>
                    <td><button className="ghost mini" onClick={() => removePasskey(p.credential_id)}>Remove</button></td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        <div style={{ marginTop: 12 }}>
          <button onClick={addPasskey} disabled={!api.passkeySupported()}>Add a passkey</button>
        </div>
        {pkMsg && <div className={`result ${pkMsg.ok ? 'ok' : 'bad'}`}>{pkMsg.text}</div>}
      </div>

      <div className="card">
        <h2>Server updates</h2>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
          Pull the latest code from <span className="mono">main</span> and restart, or just restart — no manual git pull.
          (Needs a supervisor that restarts on exit: systemd <span className="mono">Restart=always</span> / Docker <span className="mono">restart: unless-stopped</span>.)
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
