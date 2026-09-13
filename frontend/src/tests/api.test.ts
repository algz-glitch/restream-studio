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

  it('严格读取更新状态并把 AbortSignal 传给 GET /api/update', async () => {
    const payload = {
      status: 'available', current_version: '0.1.0', available_version: '0.2.0',
      release_url: 'https://github.com/algz-glitch/restream-studio/releases/tag/v0.2.0',
      last_checked_at: '2026-09-13T12:00:00Z', error_code: null, error_message: null,
    }
    const fetcher = vi.fn().mockResolvedValue(json(payload))
    const api = new ApiClient(fetcher)
    const controller = new AbortController()

    await expect(api.getUpdate(controller.signal)).resolves.toEqual(payload)
    expect(fetcher).toHaveBeenCalledWith('/api/update', expect.objectContaining({ signal: controller.signal }))
  })

  it('更新 mutation 沿用安全会话、空 JSON 请求体和 AbortSignal', async () => {
    const update = {
      status: 'current', current_version: '0.1.0', available_version: null, release_url: null,
      last_checked_at: '2026-09-13T12:00:00Z', error_code: null, error_message: null,
    }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json({ session_token: 'update-session' }))
      .mockResolvedValueOnce(json(update))
      .mockResolvedValueOnce(json({ ...update, status: 'ready', available_version: '0.2.0' }))
      .mockResolvedValueOnce(json({ status: 'restart_scheduled' }))
    const api = new ApiClient(fetcher)
    const controller = new AbortController()
    await api.session()

    await api.checkUpdate(controller.signal)
    await api.downloadUpdate(controller.signal)
    await api.installUpdate(controller.signal)

    for (const call of fetcher.mock.calls.slice(1)) {
      expect(call[1]).toEqual(expect.objectContaining({
        method: 'POST', body: '{}', signal: controller.signal,
        headers: expect.objectContaining({ 'X-Restream-Session': 'update-session' }),
      }))
    }
  })

  it.each([
    ['checkUpdate', (api: ApiClient) => api.checkUpdate()],
    ['downloadUpdate', (api: ApiClient) => api.downloadUpdate()],
    ['installUpdate', (api: ApiClient) => api.installUpdate()],
  ] as const)('%s 在会话初始化前拒绝请求且不发送空会话头', async (_name, invoke) => {
    const fetcher = vi.fn()
    const api = new ApiClient(fetcher)

    await expect(invoke(api)).rejects.toMatchObject({ code: 'session_required', message: '安全会话尚未初始化' })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it.each([
    { status: 'private', current_version: '0.1.0', available_version: null, release_url: null, last_checked_at: null, error_code: null, error_message: null },
    { status: 'idle', current_version: 1, available_version: null, release_url: null, last_checked_at: null, error_code: null, error_message: null },
    { status: 'idle', current_version: '0.1.0', available_version: null, release_url: null, last_checked_at: 1, error_code: null, error_message: null },
    { status: 'idle', current_version: '0.1.0', available_version: null, release_url: null, last_checked_at: null, error_code: null, error_message: null, local_path: 'C:\\private\\update.exe' },
  ])('拒绝字段或类型不匹配的更新响应 %#', async (payload) => {
    const api = new ApiClient(vi.fn().mockResolvedValue(json(payload)))
    await expect(api.getUpdate()).rejects.toMatchObject({ code: 'response_invalid', message: '服务器响应无效' })
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
