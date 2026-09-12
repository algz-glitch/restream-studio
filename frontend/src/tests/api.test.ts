import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError, isAbortError } from '../api'
import { json } from './fixtures'

describe('ApiClient', () => {
  it('会以 no-cache 获取会话并为 mutation 附加会话和 ETag 请求头', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json({ session_token: 'private-token' }))
      .mockResolvedValueOnce(json({ configured: false, room_identity: null, preferred_quality: null }, { headers: { ETag: '"4"' } }))
      .mockResolvedValueOnce(json({ configured: true, room_identity: 'https://live.douyin.com/1', preferred_quality: 'origin' }, { headers: { ETag: '"5"' } }))
    const api = new ApiClient(fetcher)
    await api.session()
    await api.getSource()
    await api.saveSource({ room_url: 'https://live.douyin.com/1', preferred_quality: 'origin' })
    expect(fetcher).toHaveBeenNthCalledWith(1, '/api/session', expect.objectContaining({ cache: 'no-store' }))
    expect(fetcher).toHaveBeenNthCalledWith(3, '/api/source', expect.objectContaining({
      method: 'PUT', headers: expect.objectContaining({ 'X-Restream-Session': 'private-token', 'If-Match': '"4"' }),
    }))
    expect(JSON.stringify(fetcher.mock.calls)).not.toContain('Origin')
  })

  it.each([409, 412, 422])('保留 %s 结构化错误与字段', async (status: number) => {
    const fetcher = vi.fn().mockResolvedValue(json({ error: { code: 'validation_error', message: 'bad', fields: { room_url: 'invalid' }, request_id: 'r1' } }, { status }))
    const api = new ApiClient(fetcher)
    await expect(api.getStatus()).rejects.toMatchObject({ status, code: 'validation_error', fields: { room_url: 'invalid' } } satisfies Partial<ApiError>)
  })

  it('把 AbortSignal 传入 fetch', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ desired_running: false, source_state: 'OFFLINE', source_failure: null, outputs: [] }))
    const api = new ApiClient(fetcher)
    const controller = new AbortController()
    await api.getStatus(controller.signal)
    expect(fetcher).toHaveBeenCalledWith('/api/status', expect.objectContaining({ signal: controller.signal }))
  })

  it.each([
    ['session', (api: ApiClient) => api.session(), { session_token: { secret: 'private-session' } }],
    ['getSource', (api: ApiClient) => api.getSource(), { configured: 'yes', room_identity: 'private-source', preferred_quality: null }],
    ['saveSource', (api: ApiClient) => api.saveSource({ room_url: 'https://live.douyin.com/1', preferred_quality: null }), { configured: true, room_identity: 7, preferred_quality: null }],
    ['getDestination', (api: ApiClient) => api.getDestination('douyin'), { kind: 'douyin', configured: true, masked_stream_key: 'private-key', enabled: true, status: 'LIVE' }],
    ['saveDestination', (api: ApiClient) => api.saveDestination('douyin', { enabled: true }), { kind: 'other', configured: true, masked_stream_key: null, enabled: true, status: 'LIVE' }],
    ['getStatus', (api: ApiClient) => api.getStatus(), { desired_running: false, source_state: 'OFFLINE', source_failure: null, outputs: [{ kind: 'douyin', enabled: 'yes', status: 'OFFLINE', input: 'NONE', last_error: null }] }],
    ['getEvents', (api: ApiClient) => api.getEvents(), { items: [{ id: 'private-event-id', created_at: 'now', level: 'INFO', event_type: 'test', payload: {} }], next_cursor: null }],
    ['start', (api: ApiClient) => api.start(), { status: 'private-control-state' }],
    ['stop', (api: ApiClient) => api.stop(), { status: false }],
    ['reconnect', (api: ApiClient) => api.reconnect('douyin'), { kind: 'douyin', status: 'private-action-state' }],
    ['testDestination', (api: ApiClient) => api.testDestination('douyin'), { kind: 'douyin', ok: true, diagnostic: 'private-diagnostic' }],
  ] as const)('拒绝 %s 的畸形 2xx payload 且只返回固定脱敏错误', async (_name, invoke, payload) => {
    const api = new ApiClient(vi.fn().mockResolvedValue(json(payload)))

    let caught: unknown
    try { await invoke(api) } catch (error) { caught = error }

    expect(caught).toBeInstanceOf(ApiError)
    expect(caught).toMatchObject({ status: 200, code: 'response_invalid', message: '服务器响应无效', fields: {}, requestId: '' })
    expect(JSON.stringify(caught)).not.toContain('private')
  })

  it('统一识别 DOMException 和跨运行时 AbortError', () => {
    expect(isAbortError(new DOMException('aborted', 'AbortError'))).toBe(true)
    expect(isAbortError(Object.assign(new Error('aborted'), { name: 'AbortError' }))).toBe(true)
    expect(isAbortError(new Error('network failed'))).toBe(false)
  })

  it('不依赖错误原型并拒绝非取消值', () => {
    expect(isAbortError({ name: 'AbortError' })).toBe(true)
    for (const value of [null, undefined, 'AbortError', 0, {}, { name: 'TypeError' }]) {
      expect(isAbortError(value)).toBe(false)
    }
  })
})
