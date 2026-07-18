import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { fmtTime, useGuard, useInterval, SEV_ORDER } from '../common'
import { replayCrt } from '../CrtBoot'
import type { MqttSettings, MqttClientSettings, NotifySettings, Severity } from '../types'

const SEVS = SEV_ORDER.slice(0, -1)

const EMPTY_NOTIFY: NotifySettings = {
  enabled: false, host: '', port: 587, security: 'starttls', username: '',
  from_addr: '', to_addr: '', min_sev: 'crit', has_password: false,
}
const EMPTY_MQTT: MqttSettings = { enabled: false, port: 1883, username: '', has_password: false, prefix: 'watchtower/', discovery: true }
const EMPTY_MQTT_CLIENT: MqttClientSettings = { enabled: false, host: '', port: 1883, username: '', has_password: false, tls: false, prefix: 'watchtower/', discovery: true }

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
  const [mqtt, setMqtt] = useState<MqttSettings>(EMPTY_MQTT)
  const [mqttPw, setMqttPw] = useState('')
  const [mqttMsg, setMqttMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [mqttC, setMqttC] = useState<MqttClientSettings>(EMPTY_MQTT_CLIENT)
  const [mqttCPw, setMqttCPw] = useState('')
  const [mqttCMsg, setMqttCMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [mqttCStat, setMqttCStat] = useState<{ connected: boolean; last_error?: string } | null>(null)
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
      setMqtt(d.mqtt || EMPTY_MQTT)
      setMqttC(d.mqtt_client || EMPTY_MQTT_CLIENT)
    }
    loadPasskeys()
  }, [guard, loadPasskeys])
  useEffect(() => { load() }, [load])

  // Live MQTT-client link status so "Connecting…" reflects reality.
  useInterval(() => {
    if (!mqttC.enabled) { setMqttCStat(null); return }
    api.getConfig().then((d) => setMqttCStat({
      connected: !!d.mqtt_client?.connected, last_error: d.mqtt_client?.last_error,
    })).catch(() => {})
  }, 3000)

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
  const M = (patch: Partial<MqttSettings>) => setMqtt({ ...mqtt, ...patch })
  async function saveMqtt() {
    setMqttMsg({ ok: true, text: 'Applying…' })
    const payload: Record<string, unknown> = { enabled: mqtt.enabled, port: mqtt.port, username: mqtt.username, prefix: mqtt.prefix, discovery: mqtt.discovery }
    if (mqttPw) payload.password = mqttPw
    const res = await guard(api.setConfig({ mqtt: payload }))
    if (res?.ok) { setMqttMsg({ ok: true, text: mqtt.enabled ? 'Broker (re)started.' : 'Saved (broker off).' }); setMqttPw(''); load() }
    else setMqttMsg({ ok: false, text: 'Failed.' })
  }

  const MC = (patch: Partial<MqttClientSettings>) => setMqttC({ ...mqttC, ...patch })
  async function saveMqttC() {
    setMqttCMsg({ ok: true, text: 'Applying…' })
    const payload: Record<string, unknown> = {
      enabled: mqttC.enabled, host: mqttC.host, port: mqttC.port, username: mqttC.username,
      tls: mqttC.tls, prefix: mqttC.prefix, discovery: mqttC.discovery,
    }
    if (mqttCPw) payload.password = mqttCPw
    const res = await guard(api.setConfig({ mqtt_client: payload }))
    if (res?.ok) { setMqttCMsg({ ok: true, text: mqttC.enabled ? 'Saved, see link status below.' : 'Saved (client off).' }); setMqttCPw(''); load() }
    else setMqttCMsg({ ok: false, text: 'Failed.' })
  }

  // Poll until the server is back. On an update, pull the persisted git log and show it in
  // place, staying on this tab (no reload) so the output sticks until the page is refreshed.
  async function waitForRestart(showLog: boolean) {
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 1000))
      if (await api.serverUp()) {
        if (showLog) {
          const d = await api.updateLog().catch(() => null)
          setUpdateLog((d?.log?.trim() || 'Server back up.') + '\n\n(refresh the page to load any dashboard changes)')
        } else setUpdateLog((l) => (l ?? '') + '\nServer back up.')
        setUpdating(false)
        return
      }
    }
    setUpdateLog((l) => (l ?? '') + '\nStill waiting, reload manually once it’s back.')
    setUpdating(false)
  }
  async function doUpdate() {
    if (!confirm('Pull the latest code from main and restart the server now?')) return
    setUpdating(true); setUpdateLog('Updating…')
    try {
      const d = await guard(api.updateServer())
      if (!d) { setUpdating(false); return }
      setUpdateLog('Update started. Pulling latest and restarting, waiting for the server to come back…')
    } catch { setUpdateLog('Server restarting, reconnecting…') }
    waitForRestart(true)
  }
  async function doRestart() {
    if (!confirm('Restart the server now (no code pull)?')) return
    setUpdating(true); setUpdateLog('Restarting…')
    try { await guard(api.restartServer()) } catch { /* connection may drop */ }
    waitForRestart(false)
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
          <div><label>Password {notify.has_password && '(set, blank keeps)'}</label><input type="password" value={smtpPw} autoComplete="new-password" onChange={(e) => setSmtpPw(e.target.value)} /></div>
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
        <h2>MQTT broker (hosted here)</h2>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
          Watchtower runs the MQTT broker so external systems (Home Assistant, scripts) publish print jobs to reliable
          server infra, and the printer just receives them over its existing link. Publish JSON to
          <span className="mono"> {mqtt.prefix}print</span> or <span className="mono">{mqtt.prefix}alert</span>.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, textTransform: 'none' }}>
          <input type="checkbox" checked={mqtt.enabled} onChange={(e) => M({ enabled: e.target.checked })} style={{ width: 'auto' }} /> Enable MQTT broker
        </label>
        <div className="row">
          <div><label>Port</label><input type="number" value={mqtt.port} onChange={(e) => M({ port: +e.target.value })} /></div>
          <div><label>Topic prefix</label><input value={mqtt.prefix} onChange={(e) => M({ prefix: e.target.value })} /></div>
        </div>
        <div className="row">
          <div><label>Username (blank = anonymous)</label><input value={mqtt.username} autoComplete="off" onChange={(e) => M({ username: e.target.value })} /></div>
          <div><label>Password {mqtt.has_password && '(set, blank keeps)'}</label><input type="password" value={mqttPw} autoComplete="new-password" onChange={(e) => setMqttPw(e.target.value)} /></div>
        </div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, textTransform: 'none', marginTop: 8 }}>
          <input type="checkbox" checked={mqtt.discovery} onChange={(e) => M({ discovery: e.target.checked })} style={{ width: 'auto' }} /> Publish HA discovery (auto-creates the device for any HA that connects here)
        </label>
        <div style={{ marginTop: 14 }}><button onClick={saveMqtt}>Save &amp; apply</button></div>
        {mqttMsg && <div className={`result ${mqttMsg.ok ? 'ok' : 'bad'}`}>{mqttMsg.text}</div>}
        <p className="muted" style={{ fontSize: 11, marginTop: 10 }}>
          Expose the port through your firewall/proxy for external publishers. With no username it accepts anonymous connections (LAN only).
        </p>
      </div>

      <div className="card">
        <h2>MQTT client (connect to your broker)</h2>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
          Instead of hosting a broker, Watchtower connects OUT to a broker you already run (e.g. Home Assistant's
          Mosquitto), publishes Home Assistant <b>auto-discovery</b> so a <span className="mono">Watchtower Printer</span> device
          appears on its own, and relays anything published to <span className="mono">{mqttC.prefix}print</span> to the printer.
          Runs alongside the hosted broker, use either or both.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, textTransform: 'none' }}>
          <input type="checkbox" checked={mqttC.enabled} onChange={(e) => MC({ enabled: e.target.checked })} style={{ width: 'auto' }} /> Enable MQTT client
        </label>
        <div className="row">
          <div><label>Broker host</label><input value={mqttC.host} placeholder="e.g. 192.168.1.10 or your-ha.example.com" onChange={(e) => MC({ host: e.target.value })} /></div>
          <div><label>Port</label><input type="number" value={mqttC.port} onChange={(e) => MC({ port: +e.target.value })} /></div>
        </div>
        <div className="row">
          <div><label>Username</label><input value={mqttC.username} autoComplete="off" onChange={(e) => MC({ username: e.target.value })} /></div>
          <div><label>Password {mqttC.has_password && '(set, blank keeps)'}</label><input type="password" value={mqttCPw} autoComplete="new-password" onChange={(e) => setMqttCPw(e.target.value)} /></div>
        </div>
        <div className="row">
          <div><label>Topic prefix</label><input value={mqttC.prefix} onChange={(e) => MC({ prefix: e.target.value })} /></div>
          <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', gap: 6 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, textTransform: 'none' }}>
              <input type="checkbox" checked={mqttC.tls} onChange={(e) => MC({ tls: e.target.checked })} style={{ width: 'auto' }} /> Use TLS (mqtts)
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, textTransform: 'none' }}>
              <input type="checkbox" checked={mqttC.discovery} onChange={(e) => MC({ discovery: e.target.checked })} style={{ width: 'auto' }} /> Publish HA discovery
            </label>
          </div>
        </div>
        <div style={{ marginTop: 14 }}><button onClick={saveMqttC}>Save &amp; apply</button></div>
        {mqttCMsg && <div className={`result ${mqttCMsg.ok ? 'ok' : 'bad'}`}>{mqttCMsg.text}</div>}
        {mqttC.enabled && mqttCStat && (
          <div style={{ marginTop: 8, fontSize: 13 }}>
            <span className={`dot ${mqttCStat.connected ? 'on' : ''}`} />{' '}
            {mqttCStat.connected
              ? <b>Connected to your broker.</b>
              : <span>Not connected{mqttCStat.last_error ? <>, <span className="mono">{mqttCStat.last_error}</span></> : ', connecting/retrying…'}</span>}
          </div>
        )}
        <p className="muted" style={{ fontSize: 11, marginTop: 10 }}>
          Point Home Assistant's MQTT integration at the same broker. Then call <span className="mono">notify.watchtower_printer</span> -
          no password needed (Watchtower relays as a trusted source). The printer must be online for prints to actually land.
        </p>
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
          Pull the latest code from <span className="mono">main</span> and restart, or just restart, no manual git pull.
          (Needs a supervisor that restarts on exit: systemd <span className="mono">Restart=always</span> / Docker <span className="mono">restart: unless-stopped</span>.)
        </p>
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <button style={{ flex: '0 0 auto' }} onClick={doUpdate} disabled={updating}>Pull latest &amp; restart</button>
          <button className="ghost" style={{ flex: '0 0 auto' }} onClick={doRestart} disabled={updating}>Restart service</button>
        </div>
        {updateLog !== null && <pre className="updatelog mono">{updateLog}</pre>}
      </div>

      <div className="card">
        <h2>Appearance</h2>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 12px' }}>
          Replay the CRT power-on animation. It also plays once after each successful login.
        </p>
        <div className="row">
          <button className="ghost" style={{ flex: '0 0 auto' }} onClick={replayCrt}>Play boot animation</button>
        </div>
      </div>
    </>
  )
}
