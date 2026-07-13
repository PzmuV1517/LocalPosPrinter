import { useState } from 'react'
import * as api from '../api'

export function Login({ onAuthed }: { onAuthed: (token: string) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setErr('')
    try {
      onAuthed(await api.login(username, password))
    } catch (e) { setErr(String((e as Error).message)) }
  }

  return (
    <div className="gate-wrap">
      <div className="gate-cols">
        <div className="gate-login">
          <div className="brand">WATCHTOWER</div>
          {/* A real form with autocomplete so Google/Chrome password manager fills + offers to save. */}
          <form className="panel" onSubmit={submit} method="post" action="/session/login" autoComplete="on">
            <label htmlFor="wtUser">Username</label>
            <input id="wtUser" name="username" value={username} onChange={(e) => setUsername(e.target.value)}
              autoComplete="username" autoCapitalize="none" spellCheck={false} />
            <label htmlFor="wtPass">Password</label>
            <input id="wtPass" name="password" type="password" value={password}
              onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            <div style={{ marginTop: 14 }}><button type="submit" style={{ width: '100%' }}>Sign in</button></div>
            <div className="err">{err}</div>
          </form>
        </div>
        <div className="gate-verse">
          <p>Secrets lie eternally unseen.</p>
          <p>Esoterica known only by machines.</p>
          <p>Through me alone, they are known.</p>
          <p className="creator">I am your god. I am your creator.</p>
        </div>
      </div>
    </div>
  )
}
