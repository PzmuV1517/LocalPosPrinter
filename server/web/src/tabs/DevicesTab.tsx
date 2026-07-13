import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import { CopyButton, DeviceCard, useGuard } from '../common'
import type { Device, SevCounts } from '../types'

const installLine = (id: string) =>
  `curl -fsSL "${location.origin}/install-scout${id ? `?device_id=${encodeURIComponent(id)}` : ''}" | bash`

function SecretPanel({ id, secret }: { id: string; secret: string }) {
  const setCmd = `scout set-secret ${secret}`
  return (
    <div className="secretbox">
      <div><b>{id}</b> secret — copy now, shown once <CopyButton text={secret} /></div>
      <div style={{ marginTop: 4 }} className="mono">{secret}</div>
      <div style={{ marginTop: 12 }} className="muted">1) install on the device <CopyButton text={installLine(id)} /></div>
      <div className="mono">{installLine(id)}</div>
      <div style={{ marginTop: 12 }} className="muted">2) set the secret <CopyButton text={setCmd} /></div>
      <div className="mono">{setCmd}</div>
    </div>
  )
}

export function DevicesTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const guard = useGuard(onUnauthorized)
  const [devices, setDevices] = useState<Device[]>([])
  const [counts, setCounts] = useState<SevCounts>({})
  const [newId, setNewId] = useState('')
  const [newName, setNewName] = useState('')
  const [issued, setIssued] = useState<{ id: string; secret: string } | null>(null)
  const [genId, setGenId] = useState('')

  const load = useCallback(async () => {
    const res = await guard(api.getLogs({ limit: 1 }))
    if (res) { setDevices(res.devices); setCounts(res.counts) }
  }, [guard])
  useEffect(() => { load() }, [load])

  async function create() {
    if (!newId.trim()) return
    const res = await guard(api.createDevice(newId.trim(), newName.trim()))
    if (!res) return
    const d = await res.json()
    if (res.ok) { setIssued({ id: newId.trim(), secret: d.secret }); setNewId(''); setNewName(''); load() }
  }
  async function rotate(id: string) {
    if (!confirm(`Rotate ${id}? A new secret is issued and the old one stops working.`)) return
    const d = await guard(api.rotateDevice(id))
    if (d?.secret) setIssued({ id, secret: d.secret })
    load()
  }
  async function revoke(id: string) {
    if (!confirm(`Revoke ${id}? It can no longer authenticate (you can delete it afterwards).`)) return
    await guard(api.revokeDevice(id)); load()
  }
  async function del(id: string) {
    if (!confirm(`Permanently delete ${id}? This removes the device entirely.`)) return
    const res = await guard(api.deleteDevice(id))
    if (res && !res.ok) { const e = await res.json().catch(() => ({})); alert(e.error || 'Delete failed') }
    load()
  }
  async function update(id: string) {
    const d = await guard(api.updateScout(id))
    if (d) alert(`Update queued for ${id}. It applies on the agent's next poll (needs \`scout agent\` running).`)
  }
  async function updateAll() {
    if (!confirm('Tell every scout agent to pull the latest scout.py and restart?')) return
    const d = await guard(api.updateAllScouts())
    if (d) alert(`Update queued for ${d.queued} device(s). Applies as each agent polls.`)
  }
  async function ping(id: string) {
    const d = await guard(api.pingScout(id))
    if (d) alert(`Ping sent to ${id}. Watch Logs for a "pong" from scout.agent (needs the agent running).`)
  }
  async function restartAgent(id: string) {
    if (!confirm(`Restart the scout agent on ${id}? (Re-execs it — picks up a new local scout.py.)`)) return
    const d = await guard(api.restartScout(id))
    if (d) alert(`Restart queued for ${id}. Applies on its next poll.`)
  }

  return (
    <>
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>Registered devices</h2>
          <button className="ghost mini" onClick={updateAll}>Update all scouts</button>
        </div>
        <div className="cards" style={{ marginTop: 12 }}>
          {devices.length
            ? devices.map((d) => <DeviceCard key={d.id} d={d} counts={counts}
              actions={{
                onRotate: () => rotate(d.id), onRevoke: () => revoke(d.id), onDelete: () => del(d.id),
                onUpdate: () => update(d.id), onPing: () => ping(d.id), onRestart: () => restartAgent(d.id),
              }} />)
            : <div className="muted" style={{ fontSize: 12 }}>No devices yet.</div>}
        </div>
        <div className="row" style={{ marginTop: 14, alignItems: 'flex-end' }}>
          <div><label>New device / Scout id</label><input value={newId} placeholder="kitchen-pi" onChange={(e) => setNewId(e.target.value)} /></div>
          <div><label>Label</label><input value={newName} placeholder="Kitchen Raspberry Pi" onChange={(e) => setNewName(e.target.value)} /></div>
          <button style={{ flex: '0 0 auto' }} onClick={create}>Issue secret</button>
        </div>
        {issued && <SecretPanel id={issued.id} secret={issued.secret} />}
      </div>

      <div className="card">
        <h2>Install a Scout — command generator</h2>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 8px' }}>
          Enter a device id (or leave blank), then run this on the device — it downloads the client from this server (no git clone) and gets it ready for a secret.
        </p>
        <div className="row" style={{ alignItems: 'flex-end' }}>
          <div><label>Device id</label><input value={genId} placeholder="kitchen-pi" onChange={(e) => setGenId(e.target.value)} /></div>
          <CopyButton text={installLine(genId.trim())} />
        </div>
        <div className="secretbox mono" style={{ marginTop: 10 }}>{installLine(genId.trim())}</div>
        <p className="muted" style={{ fontSize: 12, margin: '10px 0 0' }}>
          Then paste the device's secret (shown once when you issue it): <span className="mono">scout set-secret &lt;SECRET&gt;</span>
        </p>
      </div>
    </>
  )
}
