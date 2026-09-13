import type { ActionResponse, ApiErrorBody, ControlResponse, DestinationKind, DestinationResponse, DestinationUpdate, EventItem, EventsResponse, InstallResponse, Resource, SessionResponse, SourceResponse, SourceUpdate, StatusOutput, StatusResponse, UpdateResponse, UpdateStatus } from './types'

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
type Validator<T> = (value: unknown) => value is T

const destinationKinds: readonly DestinationKind[] = ['douyin', 'wechat_channels']
const updateStatuses: readonly UpdateStatus[] = ['idle', 'checking', 'current', 'available', 'downloading', 'ready', 'failed']

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value)
  return actual.length === keys.length && keys.every((key) => Object.hasOwn(value, key))
}

function isOptionalFiniteNumber(value: unknown): boolean {
  return value === undefined || (typeof value === 'number' && Number.isFinite(value))
}

function isDestinationKind(value: unknown): value is DestinationKind {
  return destinationKinds.includes(value as DestinationKind)
}

const isSessionResponse: Validator<SessionResponse> = (value): value is SessionResponse => (
  isRecord(value) && typeof value.session_token === 'string'
)

const isSourceResponse: Validator<SourceResponse> = (value): value is SourceResponse => (
  isRecord(value)
  && typeof value.configured === 'boolean'
  && isNullableString(value.room_identity)
  && isNullableString(value.preferred_quality)
)

function destinationValidator(expectedKind: DestinationKind): Validator<DestinationResponse> {
  return (value): value is DestinationResponse => (
    isRecord(value)
    && value.kind === expectedKind
    && isDestinationKind(value.kind)
    && typeof value.configured === 'boolean'
    && (value.masked_stream_key === null || value.masked_stream_key === '********')
    && typeof value.enabled === 'boolean'
    && typeof value.status === 'string'
  )
}

function isStatusOutput(value: unknown): value is StatusOutput {
  return isRecord(value)
    && typeof value.kind === 'string'
    && typeof value.enabled === 'boolean'
    && typeof value.status === 'string'
    && typeof value.input === 'string'
    && isNullableString(value.last_error)
    && isOptionalFiniteNumber(value.fps)
    && isOptionalFiniteNumber(value.bitrate_kbps)
    && isOptionalFiniteNumber(value.speed)
    && isOptionalFiniteNumber(value.uptime_seconds)
    && isOptionalFiniteNumber(value.reconnect_count)
}

const isStatusResponse: Validator<StatusResponse> = (value): value is StatusResponse => (
  isRecord(value)
  && typeof value.desired_running === 'boolean'
  && typeof value.source_state === 'string'
  && isNullableString(value.source_failure)
  && Array.isArray(value.outputs)
  && value.outputs.every(isStatusOutput)
  && isOptionalFiniteNumber(value.source_fps)
  && isOptionalFiniteNumber(value.source_bitrate_kbps)
  && isOptionalFiniteNumber(value.source_speed)
  && isOptionalFiniteNumber(value.uptime_seconds)
)

function isEventItem(value: unknown): value is EventItem {
  return isRecord(value)
    && typeof value.id === 'number'
    && Number.isInteger(value.id)
    && typeof value.created_at === 'string'
    && typeof value.level === 'string'
    && typeof value.event_type === 'string'
    && isRecord(value.payload)
}

const isEventsResponse: Validator<EventsResponse> = (value): value is EventsResponse => (
  isRecord(value)
  && Array.isArray(value.items)
  && value.items.every(isEventItem)
  && (value.next_cursor === null || (typeof value.next_cursor === 'number' && Number.isInteger(value.next_cursor)))
)

const updateKeys = ['status', 'current_version', 'available_version', 'release_url', 'last_checked_at', 'error_code', 'error_message'] as const
const isUpdateResponse: Validator<UpdateResponse> = (value): value is UpdateResponse => (
  isRecord(value)
  && hasExactKeys(value, updateKeys)
  && typeof value.status === 'string'
  && updateStatuses.includes(value.status as UpdateStatus)
  && typeof value.current_version === 'string'
  && isNullableString(value.available_version)
  && isNullableString(value.release_url)
  && isNullableString(value.last_checked_at)
  && isNullableString(value.error_code)
  && isNullableString(value.error_message)
)

const isInstallResponse: Validator<InstallResponse> = (value): value is InstallResponse => (
  isRecord(value) && hasExactKeys(value, ['status']) && value.status === 'restart_scheduled'
)

function controlValidator(expectedStatus: ControlResponse['status']): Validator<ControlResponse> {
  return (value): value is ControlResponse => isRecord(value) && value.status === expectedStatus
}

function actionValidator(expectedKind: DestinationKind, action: 'reconnect' | 'test'): Validator<ActionResponse> {
  return (value): value is ActionResponse => {
    if (!isRecord(value) || value.kind !== expectedKind || !isDestinationKind(value.kind)) return false
    if (action === 'reconnect') return value.status === 'reconnect_requested'
    return typeof value.ok === 'boolean' && (value.diagnostic === 'connection_valid' || value.diagnostic === 'connection_failed')
  }
}

export function isAbortError(value: unknown): boolean {
  return isRecord(value) && value.name === 'AbortError'
}

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string, public readonly fields: Record<string, string>, public readonly requestId: string) {
    super(message)
    this.name = 'ApiError'
  }
}

function isErrorBody(value: unknown): value is ApiErrorBody {
  if (!value || typeof value !== 'object' || !('error' in value)) return false
  const error = (value as { error?: unknown }).error
  return Boolean(error && typeof error === 'object' && 'code' in error && 'message' in error)
}

export class ApiClient {
  private token = ''
  private readonly etags = new Map<string, string>()
  constructor(private readonly fetcher: Fetcher = (input, init) => globalThis.fetch(input, init)) {}

  private async request<T>(path: string, validator: Validator<T>, init: RequestInit = {}, resource?: string): Promise<T> {
    const response = await this.fetcher(path, { credentials: 'same-origin', ...init })
    let payload: unknown = null
    try { payload = await response.json() } catch { payload = null }
    if (!response.ok) {
      if (isErrorBody(payload)) throw new ApiError(response.status, payload.error.code, payload.error.message, payload.error.fields ?? {}, payload.error.request_id ?? '')
      throw new ApiError(response.status, 'request_failed', '请求未完成', {}, '')
    }
    if (!validator(payload)) throw new ApiError(response.status, 'response_invalid', '服务器响应无效', {}, '')
    if (resource) {
      const etag = response.headers.get('ETag')
      if (etag) this.etags.set(resource, etag)
    }
    return payload
  }

  private mutation<T>(path: string, method: 'POST' | 'PUT', body: object, validator: Validator<T>, signal?: AbortSignal, resource?: string): Promise<T> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json', 'X-Restream-Session': this.token }
    if (resource) headers['If-Match'] = this.etags.get(resource) ?? '"0"'
    return this.request(path, validator, { method, headers, body: JSON.stringify(body), signal }, resource)
  }

  async session(signal?: AbortSignal): Promise<SessionResponse> {
    const value = await this.request('/api/session', isSessionResponse, { cache: 'no-store', signal })
    this.token = value.session_token
    return value
  }
  getSource(signal?: AbortSignal): Promise<Resource<SourceResponse>> {
    return this.fetchResource('/api/source', 'source', isSourceResponse, signal)
  }
  getDestination(kind: DestinationKind, signal?: AbortSignal): Promise<Resource<DestinationResponse>> {
    return this.fetchResource(`/api/destinations/${kind}`, `destination:${kind}`, destinationValidator(kind), signal)
  }
  private async fetchResource<T>(path: string, resource: string, validator: Validator<T>, signal?: AbortSignal): Promise<Resource<T>> {
    const data = await this.request(path, validator, { signal }, resource)
    return { data, etag: this.etags.get(resource) ?? '"0"' }
  }
  saveSource(value: SourceUpdate, signal?: AbortSignal): Promise<SourceResponse> { return this.mutation('/api/source', 'PUT', value, isSourceResponse, signal, 'source') }
  saveDestination(kind: DestinationKind, value: DestinationUpdate, signal?: AbortSignal): Promise<DestinationResponse> { return this.mutation(`/api/destinations/${kind}`, 'PUT', value, destinationValidator(kind), signal, `destination:${kind}`) }
  getStatus(signal?: AbortSignal): Promise<StatusResponse> { return this.request('/api/status', isStatusResponse, { signal }) }
  getUpdate(signal?: AbortSignal): Promise<UpdateResponse> { return this.request('/api/update', isUpdateResponse, { signal }) }
  checkUpdate(signal?: AbortSignal): Promise<UpdateResponse> { return this.mutation('/api/update/check', 'POST', {}, isUpdateResponse, signal) }
  downloadUpdate(signal?: AbortSignal): Promise<UpdateResponse> { return this.mutation('/api/update/download', 'POST', {}, isUpdateResponse, signal) }
  installUpdate(signal?: AbortSignal): Promise<InstallResponse> { return this.mutation('/api/update/install', 'POST', {}, isInstallResponse, signal) }
  getEvents(cursor = 0, signal?: AbortSignal): Promise<EventsResponse> { return this.request(`/api/events?limit=50&cursor=${cursor}`, isEventsResponse, { signal }) }
  start(signal?: AbortSignal): Promise<ControlResponse> { return this.mutation('/api/control/start', 'POST', { local_test: false }, controlValidator('started'), signal) }
  stop(signal?: AbortSignal): Promise<ControlResponse> { return this.mutation('/api/control/stop', 'POST', {}, controlValidator('stopped'), signal) }
  reconnect(kind: DestinationKind, signal?: AbortSignal): Promise<ActionResponse> { return this.mutation(`/api/destinations/${kind}/reconnect`, 'POST', {}, actionValidator(kind, 'reconnect'), signal) }
  testDestination(kind: DestinationKind, signal?: AbortSignal): Promise<ActionResponse> { return this.mutation(`/api/destinations/${kind}/test`, 'POST', {}, actionValidator(kind, 'test'), signal) }
}

export const api = new ApiClient()
