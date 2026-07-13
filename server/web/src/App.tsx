import { useCallback, useEffect, useState } from 'react'
import * as api from './api'
import { Setup } from './screens/Setup'
import { Login } from './screens/Login'
import { Dashboard } from './screens/Dashboard'

type Screen = 'loading' | 'setup' | 'gate' | 'app'

export function App() {
  const [screen, setScreen] = useState<Screen>('loading')

  const boot = useCallback(async () => {
    try {
      const { configured } = await api.setupStatus()
      if (!configured) { setScreen('setup'); return }
    } catch { /* if setup status fails, fall through to the gate */ }
    // A valid token in localStorage keeps us signed in across refreshes.
    setScreen((await api.verifySession()) ? 'app' : 'gate')
  }, [])

  useEffect(() => { boot() }, [boot])

  // Any 401 anywhere drops us to the gate (token cleared).
  const onUnauthorized = useCallback(() => { api.clearToken(); setScreen('gate') }, [])

  const onAuthed = useCallback((token: string) => { api.setToken(token); setScreen('app') }, [])

  if (screen === 'loading') return null
  if (screen === 'setup') return <Setup onAuthed={onAuthed} />
  if (screen === 'gate') return <Login onAuthed={onAuthed} />
  return <Dashboard onLogout={onUnauthorized} onUnauthorized={onUnauthorized} />
}
