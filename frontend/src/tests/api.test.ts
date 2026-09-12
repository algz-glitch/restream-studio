import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError } from '../api'
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
})
