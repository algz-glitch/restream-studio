export const source = { configured: true, room_identity: 'https://live.douyin.com/123456', preferred_quality: 'origin' }
export const destinations = {
  douyin: { kind: 'douyin', configured: true, masked_stream_key: '********', enabled: true, status: 'LIVE' },
  wechat_channels: { kind: 'wechat_channels', configured: true, masked_stream_key: '********', enabled: true, status: 'STANDBY' },
}
export const status = {
  desired_running: false,
  source_state: 'OFFLINE',
  source_failure: null,
  outputs: [
    { kind: 'douyin', enabled: true, status: 'OFFLINE', input: 'NONE', last_error: null },
    { kind: 'wechat_channels', enabled: true, status: 'STANDBY', input: 'NONE', last_error: null },
  ],
}
export const events = { items: [], next_cursor: null }

export function json(value: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json', ...init.headers }, ...init })
}

export function installApi(overrides: Record<string, unknown> = {}): ReturnType<typeof vi.fn> {
  const responses: Record<string, unknown> = {
    '/api/session': { session_token: 'session-test-token' },
    '/api/source': source,
    '/api/destinations/douyin': destinations.douyin,
    '/api/destinations/wechat_channels': destinations.wechat_channels,
    '/api/status': status,
    '/api/events?limit=50&cursor=0': events,
    ...overrides,
  }
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === 'string' ? input : input.toString()
    const key = `${init?.method ?? 'GET'} ${path}`
    const value = await Promise.resolve(responses[key] ?? responses[path])
    if (value instanceof Response) return value
    if (value === undefined) return json({ error: { code: 'missing_mock', message: path, fields: {}, request_id: 'test' } }, { status: 500 })
    return json(value, path.includes('/source') || path.includes('/destinations/') ? { headers: { ETag: '"1"' } } : {})
  })
  vi.stubGlobal('fetch', mock)
  return mock
}
