export type Severity =
  | 'emerg' | 'alert' | 'crit' | 'err' | 'warning' | 'notice' | 'info' | 'debug'

export interface LogRow {
  id: number
  device_id: string
  severity: Severity
  sev_num: number
  service: string
  message: string
  meta: Record<string, unknown>
  source_ip: string
  printed: boolean
  ts: number
}

export interface Camera { node: string; name: string }
export interface Guest { vmid: number; name: string; kind: 'vm' | 'ct'; status: string }

export interface Device {
  id: string
  name: string
  created_at: number
  last_seen_at: number | null
  meta: Record<string, unknown>
  revoked: boolean
  agent_online?: boolean
  heartbeat_secs?: number
}

export type SevCounts = Record<string, Partial<Record<Severity, number>>>

export interface LogsResponse {
  logs: LogRow[]
  devices: Device[]
  counts: SevCounts
  device_connected: boolean
}

export interface TempPassword {
  user: string
  max_uses: number
  used: number
  remaining: number
  revoked: boolean
  active: boolean
}

export interface HistoryRow {
  timestamp: number
  format: string
  label: string
  user: string
  status: string
}

export interface NotifySettings {
  enabled: boolean
  host: string
  port: number
  security: string
  username: string
  from_addr: string
  to_addr: string
  min_sev: Severity
  has_password: boolean
}

export interface MqttSettings {
  enabled: boolean
  port: number
  username: string
  has_password: boolean
  prefix: string
  discovery: boolean
}

export interface MqttClientSettings {
  enabled: boolean
  host: string
  port: number
  username: string
  has_password: boolean
  tls: boolean
  prefix: string
  discovery: boolean
  connected?: boolean
  last_error?: string
}

export interface ServerConfig {
  username: string
  print_width: number
  auto_print_min_sev: Severity
  auto_print_max_per_min: number
  log_retention_days: number
  err_retention_days: number
  disk_alert_pct: number
  notify: NotifySettings
  mqtt: MqttSettings
  mqtt_client: MqttClientSettings
}

export interface UpdateResult {
  ok: boolean
  changed: boolean
  before?: string
  after?: string
  restarting: boolean
  log: string
}
