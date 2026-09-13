export type DestinationKind = 'douyin' | 'wechat_channels'

export interface ApiErrorBody {
  error: { code: string; message: string; fields: Record<string, string>; request_id: string }
}

export interface SessionResponse { session_token: string }
export interface SourceResponse { configured: boolean; room_identity: string | null; preferred_quality: string | null }
export interface SourceUpdate { room_url: string; preferred_quality: string | null }
export interface DestinationResponse {
  kind: DestinationKind
  configured: boolean
  masked_stream_key: '********' | null
  enabled: boolean
  status: string
}
export interface DestinationUpdate { base_server?: string; stream_key?: string; enabled?: boolean }
export interface StatusOutput {
  kind: string
  enabled: boolean
  status: string
  input: string
  last_error: string | null
  fps?: number
  bitrate_kbps?: number
  speed?: number
  uptime_seconds?: number
  reconnect_count?: number
}
export interface StatusResponse {
  desired_running: boolean
  source_state: string
  source_failure: string | null
  outputs: StatusOutput[]
  source_fps?: number
  source_bitrate_kbps?: number
  source_speed?: number
  uptime_seconds?: number
}
export interface EventItem { id: number; created_at: string; level: string; event_type: string; payload: Record<string, unknown> }
export interface EventsResponse { items: EventItem[]; next_cursor: number | null }
export interface ControlResponse { status: 'started' | 'stopped' }
export interface ActionResponse { kind: DestinationKind; status?: string; ok?: boolean; diagnostic?: string }
export interface Resource<T> { data: T; etag: string }

export type UpdateStatus = 'idle' | 'checking' | 'current' | 'available' | 'downloading' | 'ready' | 'failed'
export interface UpdateResponse {
  status: UpdateStatus
  current_version: string
  available_version: string | null
  release_url: string | null
  last_checked_at: string | null
  error_code: string | null
  error_message: string | null
}
export interface InstallResponse { status: 'restart_scheduled' }
