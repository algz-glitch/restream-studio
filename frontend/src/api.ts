import type { ActionResponse, ApiErrorBody, ControlResponse, DestinationKind, DestinationResponse, DestinationUpdate, EventsResponse, Resource, SessionResponse, SourceResponse, SourceUpdate, StatusResponse } from './types'

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

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

  private async request<T>(path: string, init: RequestInit = {}, resource?: string): Promise<T> {
    const response = await this.fetcher(path, { credentials: 'same-origin', ...init })
    let payload: unknown = null
    try { payload = await response.json() } catch { payload = null }
    if (!response.ok) {
      if (isErrorBody(payload)) throw new ApiError(response.status, payload.error.code, payload.error.message, payload.error.fields ?? {}, payload.error.request_id ?? '')
      throw new ApiError(response.status, 'request_failed', '请求未完成', {}, '')
    }
    if (resource) {
      const etag = response.headers.get('ETag')
      if (etag) this.etags.set(resource, etag)
    }
    return payload as T
  }

  private mutation<T>(path: string, method: 'POST' | 'PUT', body: object, signal?: AbortSignal, resource?: string): Promise<T> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json', 'X-Restream-Session': this.token }
    if (resource) headers['If-Match'] = this.etags.get(resource) ?? '"0"'
    return this.request<T>(path, { method, headers, body: JSON.stringify(body), signal }, resource)
  }

  async session(signal?: AbortSignal): Promise<SessionResponse> {
    const value = await this.request<SessionResponse>('/api/session', { cache: 'no-store', signal })
    this.token = value.session_token
    return value
  }
  getSource(signal?: AbortSignal): Promise<Resource<SourceResponse>> {
    return this.fetchResource('/api/source', 'source', signal)
  }
  getDestination(kind: DestinationKind, signal?: AbortSignal): Promise<Resource<DestinationResponse>> {
    return this.fetchResource(`/api/destinations/${kind}`, `destination:${kind}`, signal)
  }
  private async fetchResource<T>(path: string, resource: string, signal?: AbortSignal): Promise<Resource<T>> {
    const data = await this.request<T>(path, { signal }, resource)
    return { data, etag: this.etags.get(resource) ?? '"0"' }
  }
  saveSource(value: SourceUpdate, signal?: AbortSignal): Promise<SourceResponse> { return this.mutation('/api/source', 'PUT', value, signal, 'source') }
  saveDestination(kind: DestinationKind, value: DestinationUpdate, signal?: AbortSignal): Promise<DestinationResponse> { return this.mutation(`/api/destinations/${kind}`, 'PUT', value, signal, `destination:${kind}`) }
  getStatus(signal?: AbortSignal): Promise<StatusResponse> { return this.request('/api/status', { signal }) }
  getEvents(cursor = 0, signal?: AbortSignal): Promise<EventsResponse> { return this.request(`/api/events?limit=50&cursor=${cursor}`, { signal }) }
  start(signal?: AbortSignal): Promise<ControlResponse> { return this.mutation('/api/control/start', 'POST', { local_test: false }, signal) }
  stop(signal?: AbortSignal): Promise<ControlResponse> { return this.mutation('/api/control/stop', 'POST', {}, signal) }
  reconnect(kind: DestinationKind, signal?: AbortSignal): Promise<ActionResponse> { return this.mutation(`/api/destinations/${kind}/reconnect`, 'POST', {}, signal) }
  testDestination(kind: DestinationKind, signal?: AbortSignal): Promise<ActionResponse> { return this.mutation(`/api/destinations/${kind}/test`, 'POST', {}, signal) }
}

export const api = new ApiClient()
