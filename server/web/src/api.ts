// Typed API client. The session token lives in localStorage and is sent as a Bearer header,
// so a page refresh keeps you logged in (App verifies it on load, clears it only when invalid).
import type {
  LogsResponse, ServerConfig, TempPassword, HistoryRow, UpdateResult,
} from './types'

const TOKEN_KEY = 'wt_token'

// When running inside the Watchtower Mobile app, a native bridge persists the token so login
// survives app restarts even if the WebView drops its localStorage. Harmless in a browser.
interface NativeBridge { getToken?: () => string; saveToken?: (t: string) => void }
const bridge = (): NativeBridge | undefined => (window as unknown as { Android?: NativeBridge }).Android

export const getToken = (): string | null => {
  let t = localStorage.getItem(TOKEN_KEY)
  if (!t) {
    const nt = bridge()?.getToken?.()
    if (nt) { t = nt; localStorage.setItem(TOKEN_KEY, nt) }  // rehydrate from native
  }
  return t
}
export const setToken = (t: string) => { localStorage.setItem(TOKEN_KEY, t); bridge()?.saveToken?.(t) }
export const clearToken = () => {
  const t = localStorage.getItem(TOKEN_KEY)
  if (t) fetch('/session/logout', { method: 'POST', headers: { Authorization: `Bearer ${t}` } }).catch(() => {})
  localStorage.removeItem(TOKEN_KEY)
  bridge()?.saveToken?.('')
}

/** Thrown on a 401 so the app can drop back to the login gate. */
export class Unauthorized extends Error {}

function authHeaders(): HeadersInit {
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken() ?? ''}` }
}

async function post<T = any>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body ?? {}) })
  if (res.status === 401) throw new Unauthorized()
  return res.json() as Promise<T>
}

/** POST that returns the raw Response (for endpoints where the status matters). */
async function postRaw(path: string, body?: unknown): Promise<Response> {
  const res = await fetch(path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body ?? {}) })
  if (res.status === 401) throw new Unauthorized()
  return res
}

// ---- unauthenticated ----
export async function setupStatus(): Promise<{ configured: boolean }> {
  return (await fetch('/setup/status')).json()
}
export async function verifySession(): Promise<boolean> {
  const t = getToken()
  if (!t) return false
  try {
    const r = await fetch('/session/verify', { headers: { Authorization: `Bearer ${t}` } })
    const d = await r.json()
    return !!d.ok
  } catch { return false }
}
export async function login(username: string, password: string): Promise<string> {
  const r = await fetch('/session/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const d = await r.json()
  if (!r.ok || !d.token) throw new Error(d.error || 'Invalid username or password')
  return d.token
}
export async function runSetup(payload: Record<string, unknown>): Promise<string> {
  const r = await fetch('/setup', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
  const d = await r.json()
  if (!r.ok || !d.token) throw new Error(d.error || 'Setup failed')
  return d.token
}
export async function serverUp(): Promise<boolean> {
  try { return (await fetch('/healthz', { cache: 'no-store' })).ok } catch { return false }
}
export const getStatus = () => post<{ device_connected: boolean; pending_jobs: number }>('/status', {})

// ---- logs / devices ----
export const getLogs = (filters: Record<string, unknown>) => post<LogsResponse>('/watchtower/logs', filters)
export const printLog = (log_id: number) => post<{ delivered: boolean; queued: boolean }>('/watchtower/print', { log_id })
export const createDevice = (device_id: string, name: string) =>
  postRaw('/watchtower/devices/create', { device_id, name })
export const rotateDevice = (device_id: string) => post<{ secret?: string }>('/watchtower/devices/rotate', { device_id })
export const revokeDevice = (device_id: string) => post('/watchtower/devices/revoke', { device_id })
export const deleteDevice = (device_id: string) => postRaw('/watchtower/devices/delete', { device_id })
export const updateScout = (device_id: string) => post<{ queued: number }>('/watchtower/devices/update', { device_id })
export const updateAllScouts = () => post<{ queued: number }>('/watchtower/devices/update', { all: true })
export const pingScout = (device_id: string) => post<{ queued: number }>('/watchtower/devices/command', { device_id, cmd: 'ping' })
export const restartScout = (device_id: string) => post<{ queued: number }>('/watchtower/devices/command', { device_id, cmd: 'restart' })
export const runOnScout = (device_id: string, command: string) => post('/watchtower/devices/run', { device_id, command })
export const setHeartbeat = (device_id: string, seconds: number) => post('/watchtower/devices/heartbeat', { device_id, seconds })
export const metricsSeries = (hours: number) =>
  post<{ start: number; width: number; buckets: number; err: number[]; other: number[] }>('/watchtower/metrics', { hours })
export const testEmail = () => post<{ ok: boolean; message: string }>('/config/test-email', {})

/** Trigger a CSV download of the current filtered logs. */
export async function exportLogsCsv(filters: Record<string, unknown>) {
  const res = await fetch('/watchtower/logs/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken() ?? ''}` },
    body: JSON.stringify({ ...filters, format: 'csv' }),
  })
  if (!res.ok) return
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url; a.download = 'watchtower-logs.csv'; a.click()
  URL.revokeObjectURL(url)
}

// ---- passwords / history ----
export const adminState = () => post<{ history: HistoryRow[]; passwords: TempPassword[] }>('/admin/state', {})
export const createPassword = (user: string, max_uses: number, new_password: string) =>
  postRaw('/admin/create', { user, max_uses, new_password })
export const checkPassword = (password: string) => post<{ valid: boolean; message: string }>('/check', { password })

// ---- config / update ----
export const getConfig = () => post<ServerConfig>('/config/get', {})
export const setConfig = (body: Record<string, unknown>) => postRaw('/config/set', body)
export const updateServer = () => post<UpdateResult>('/config/update', {})
export const restartServer = () => post<{ ok: boolean; restarting: boolean }>('/config/restart', {})

// ---- print / preview ----
export async function preview(payload: unknown): Promise<{ ok: true; url: string } | { ok: false; error: string }> {
  const r = await fetch('/preview', { method: 'POST', headers: authHeaders(), body: JSON.stringify(payload) })
  if (!r.ok) {
    const e = await r.json().catch(() => ({} as { error?: string }))
    const hint = r.status === 413 ? ' (image too large for the proxy)' : ''
    return { ok: false, error: (e.error || `Preview failed (${r.status})`) + hint }
  }
  return { ok: true, url: URL.createObjectURL(await r.blob()) }
}
export const printPayload = (payload: unknown) => postRaw('/print', payload)

// ---- WebAuthn passkeys (fingerprint / Touch ID / Windows Hello) ----
function b64urlToBuf(s: string): ArrayBuffer {
  const pad = '='.repeat((4 - (s.length % 4)) % 4)
  const bin = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'))
  const u = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i)
  return u.buffer
}
function bufToB64url(b: ArrayBuffer): string {
  const u = new Uint8Array(b)
  let s = ''
  for (const x of u) s += String.fromCharCode(x)
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}
function prepCreate(o: any) {
  o.challenge = b64urlToBuf(o.challenge)
  o.user.id = b64urlToBuf(o.user.id)
  if (o.excludeCredentials) o.excludeCredentials = o.excludeCredentials.map((c: any) => ({ ...c, id: b64urlToBuf(c.id) }))
  return o
}
function prepGet(o: any) {
  o.challenge = b64urlToBuf(o.challenge)
  if (o.allowCredentials) o.allowCredentials = o.allowCredentials.map((c: any) => ({ ...c, id: b64urlToBuf(c.id) }))
  return o
}

export function passkeySupported(): boolean {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential
}

export async function registerPasskey(label: string): Promise<void> {
  const begin = await post<{ state: string; options: any }>('/webauthn/register/begin', {})
  const cred = (await navigator.credentials.create({ publicKey: prepCreate(begin.options) })) as PublicKeyCredential
  const r = cred.response as AuthenticatorAttestationResponse
  const credential = {
    id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON),
      attestationObject: bufToB64url(r.attestationObject),
      transports: typeof r.getTransports === 'function' ? r.getTransports() : [],
    },
    clientExtensionResults: {},
  }
  const res = await postRaw('/webauthn/register/complete', { state: begin.state, credential, label })
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || 'Registration failed')
}

export async function loginWithPasskey(): Promise<string> {
  const beginRes = await fetch('/webauthn/login/begin', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  const b = await beginRes.json()
  if (!beginRes.ok) throw new Error(b.error || 'No passkeys registered')
  const cred = (await navigator.credentials.get({ publicKey: prepGet(b.options) })) as PublicKeyCredential
  const r = cred.response as AuthenticatorAssertionResponse
  const credential = {
    id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON),
      authenticatorData: bufToB64url(r.authenticatorData),
      signature: bufToB64url(r.signature),
      userHandle: r.userHandle ? bufToB64url(r.userHandle) : null,
    },
    clientExtensionResults: {},
  }
  const res = await fetch('/webauthn/login/complete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state: b.state, credential }),
  })
  const d = await res.json()
  if (!res.ok || !d.token) throw new Error(d.error || 'Passkey login failed')
  return d.token as string
}

export interface Passkey { credential_id: string; label: string; created_at: number; last_used_at: number | null }
export const listPasskeys = () => post<{ passkeys: Passkey[] }>('/webauthn/list', {})
export const deletePasskey = (credential_id: string) => post('/webauthn/delete', { credential_id })
