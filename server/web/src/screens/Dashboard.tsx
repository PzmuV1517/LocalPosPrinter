import { useState } from 'react'
import * as api from '../api'
import { useInterval } from '../common'
import { LogsTab } from '../tabs/LogsTab'
import { PrintTab } from '../tabs/PrintTab'
import { DevicesTab } from '../tabs/DevicesTab'
import { PasswordsTab } from '../tabs/PasswordsTab'
import { HistoryTab } from '../tabs/HistoryTab'
import { SettingsTab } from '../tabs/SettingsTab'

type Tab = 'logs' | 'print' | 'devices' | 'passwords' | 'history' | 'settings'
const TABS: Tab[] = ['logs', 'print', 'devices', 'passwords', 'history', 'settings']

export function Dashboard({ onLogout, onUnauthorized }: { onLogout: () => void; onUnauthorized: () => void }) {
  const [tab, setTab] = useState<Tab>('logs')
  const [connected, setConnected] = useState(false)

  useInterval(() => { api.getStatus().then((s) => setConnected(s.device_connected)).catch(() => {}) }, 4000)

  return (
    <div>
      <header>
        <div className="brand">WATCHTOWER</div>
        <div className="right">
          <span><span className={`dot ${connected ? 'on' : ''}`} />{connected ? 'printer online' : 'printer offline'}</span>
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
        {tab === 'devices' && <DevicesTab onUnauthorized={onUnauthorized} />}
        {tab === 'passwords' && <PasswordsTab onUnauthorized={onUnauthorized} />}
        {tab === 'history' && <HistoryTab onUnauthorized={onUnauthorized} />}
        {tab === 'settings' && <SettingsTab onUnauthorized={onUnauthorized} />}
      </main>
    </div>
  )
}
