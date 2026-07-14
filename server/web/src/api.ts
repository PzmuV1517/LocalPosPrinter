// Typed API client. The session token lives in localStorage and is sent as a Bearer header,
// so a page refresh keeps you logged in (App verifies it on load, clears it only when invalid).
import type {
  LogsResponse, ServerConfig, TempPassword, HistoryRow, UpdateResult,
} from './types'

const TOKEN_KEY = 'wt_token'

export const getToken = (): string | null => localStorage.getItem(TOKEN_KEY)
export const setToken = (t: string) => localStorage.setItem(TOKEN_KEY, t)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)

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
  try { return (await fetch('/status', { cache: 'no-store' })).ok } catch { return false }
}
export async function getStatus(): Promise<{ device_connected: boolean; pending_jobs: number }> {
  return (await fetch('/status', { cache: 'no-store' })).json()
}

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
  const r = await fetch('/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  if (!r.ok) { const e = await r.json().catch(() => ({})); return { ok: false, error: e.error || 'Preview error' } }
  return { ok: true, url: URL.createObjectURL(await r.blob()) }
}
export const printPayload = (payload: unknown) => postRaw('/print', payload)
