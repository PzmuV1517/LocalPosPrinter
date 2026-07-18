import { useCallback, useEffect, useState } from 'react'
import * as api from './api'
import { Setup } from './screens/Setup'
import { Login } from './screens/Login'
import { Dashboard } from './screens/Dashboard'
import { CrtBoot } from './CrtBoot'
import { Hud } from './Hud'

type Screen = 'loading' | 'setup' | 'gate' | 'app'

export function App() {
  const [screen, setScreen] = useState<Screen>('loading')
  const [reason, setReason] = useState('')
  // True only right after a fresh auth, so the CRT intro fires on login, not on session restore.
  const [freshAuth, setFreshAuth] = useState(false)

  const boot = useCallback(async () => {
    try {
      const { configured } = await api.setupStatus()
      if (!configured) { setScreen('setup'); return }
    } catch { /* if setup status fails, fall through to the gate */ }
    const t = api.getToken()
    if (!t) { setReason('no saved session (browser storage was empty)'); setScreen('gate'); return }
    const ok = await api.verifySession()
    if (ok) { setScreen('app') }
    else { setReason('saved session was rejected by the server'); setScreen('gate') }
  }, [])

  useEffect(() => { boot() }, [boot])

  // A transient 401 clears only the local token (does NOT revoke the server session).
  const onUnauthorized = useCallback(() => { api.clearToken(); setScreen('gate') }, [])
  const onLogout = useCallback(async () => { await api.logout(); setScreen('gate') }, [])
  const onAuthed = useCallback((token: string) => { api.setToken(token); setReason(''); setFreshAuth(true); setScreen('app') }, [])

  const content =
    screen === 'loading' ? null
      : screen === 'setup' ? <Setup onAuthed={onAuthed} />
        : screen === 'gate' ? <Login onAuthed={onAuthed} reason={reason} />
          : (
            <CrtBoot active={freshAuth}>
              <Dashboard onLogout={onLogout} onUnauthorized={onUnauthorized} />
            </CrtBoot>
          )

  return <>{content}<Hud /></>
}
