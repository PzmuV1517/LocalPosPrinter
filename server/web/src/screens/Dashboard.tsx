import { useState } from 'react'
import * as api from '../api'
import { useInterval } from '../common'
import { LogsTab } from '../tabs/LogsTab'
import { PrintTab } from '../tabs/PrintTab'
import { DevicesTab } from '../tabs/DevicesTab'
import { PasswordsTab } from '../tabs/PasswordsTab'
import { HistoryTab } from '../tabs/HistoryTab'
import { SettingsTab } from '../tabs/SettingsTab'
import { ConferTab } from '../tabs/ConferTab'

type Tab = 'logs' | 'print' | 'confer' | 'devices' | 'passwords' | 'history' | 'settings'
const TABS: Tab[] = ['logs', 'print', 'confer', 'devices', 'passwords', 'history', 'settings']

export function Dashboard({ onLogout, onUnauthorized }: { onLogout: () => void; onUnauthorized: () => void }) {
  const [tab, setTab] = useState<Tab>('logs')
  const [connected, setConnected] = useState(false)
  const [ready, setReady] = useState(true)
  const [pstate, setPstate] = useState('')
  const [conferMode, setConferMode] = useState(false)

  useInterval(() => {
    api.getStatus().then((s) => {
      setConnected(s.device_connected); setReady(s.printer_ready ?? true)
      setPstate(s.printer_state ?? ''); setConferMode(!!s.confer_mode)
    }).catch(() => {})
  }, 4000)

  // white = ready to print, red = connected but can't print, grey = offline.
  const dot = conferMode || (connected && ready) ? 'on' : connected ? 'busy' : 'off'
  const label = conferMode ? 'in Confer mode'
    : !connected ? 'printer offline'
    : ready ? 'printer online'
    : `printer not ready: ${pstate}`

  return (
    <div>
      <header>
        <div className="brand">WATCHTOWER</div>
        <div className="right">
          <span><span className={`dot ${dot}`} />{label}</span>
          <button className="ghost mini" onClick={onLogout}>Sign out</button>
        </div>
      </header>
      <nav className="tabs">
        {TABS.map((t) => (
          <button key={t} className={t === tab ? 'active' : ''} onClick={() => setTab(t)}>{t}</button>
        ))}
      </nav>
      <main>
        {tab === 'logs' && <LogsTab onUnauthorized={onUnauthorized} />}
        {tab === 'print' && <PrintTab onUnauthorized={onUnauthorized} />}
        {tab === 'confer' && <ConferTab onUnauthorized={onUnauthorized} />}
        {tab === 'devices' && <DevicesTab onUnauthorized={onUnauthorized} />}
        {tab === 'passwords' && <PasswordsTab onUnauthorized={onUnauthorized} />}
        {tab === 'history' && <HistoryTab onUnauthorized={onUnauthorized} />}
        {tab === 'settings' && <SettingsTab onUnauthorized={onUnauthorized} />}
      </main>
    </div>
  )
}
