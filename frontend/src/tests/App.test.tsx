import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { destinations, events, installApi, json, source, status } from './fixtures'

describe('播控台', () => {
  beforeEach(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' }) })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('加载时保留页面布局骨架，完成后呈现离线状态与移动端语义顺序', async () => {
    let resolveSource: ((value: Response) => void) | undefined
    installApi({ '/api/source': new Promise<Response>((resolve) => { resolveSource = resolve }) })
    render(<App />)
    expect(screen.getByRole('main')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getAllByTestId('skeleton').length).toBeGreaterThan(2)
    resolveSource?.(json(source, { headers: { ETag: '"1"' } }))
    expect(await screen.findByText('离线')).toBeVisible()
    const regions = screen.getAllByRole('region').map((item) => item.getAttribute('aria-labelledby'))
    expect(regions).toEqual(expect.arrayContaining(['source-title', 'douyin-title', 'wechat_channels-title', 'monitor-title', 'logs-title']))
  })

  it('空配置给出下一步并禁用开始按钮及解释原因', async () => {
    installApi({
      '/api/source': { configured: false, room_identity: null, preferred_quality: null },
      '/api/destinations/douyin': { ...destinations.douyin, configured: false, masked_stream_key: null, enabled: false },
      '/api/destinations/wechat_channels': { ...destinations.wechat_channels, configured: false, masked_stream_key: null, enabled: false },
    })
    render(<App />)
    const start = await screen.findByRole('button', { name: '开始监控' })
    expect(start).toBeDisabled()
    expect(screen.getByText('先保存抖音直播间地址，再配置并启用至少一个输出目标。')).toBeVisible()
  })

  it('已有推流密钥只显示固定掩码且输入值为空', async () => {
    installApi(); render(<App />)
    expect(await screen.findAllByText('已保存：********')).toHaveLength(2)
    for (const item of screen.getAllByLabelText('推流密钥')) expect(item).toHaveValue('')
  })

  it('校验源字段、保存 canonical 回显且请求期间禁用', async () => {
    let resolveSave: ((value: Response) => void) | undefined
    const mock = installApi({
      'PUT /api/source': new Promise<Response>((resolve) => { resolveSave = resolve }),
    })
    const user = userEvent.setup(); render(<App />)
    const input = await screen.findByLabelText('抖音直播间地址')
    await user.clear(input); await user.type(input, 'https://example.com/not-douyin')
    await user.click(screen.getByRole('button', { name: '保存来源' }))
    expect(screen.getByText('请输入支持的抖音直播间地址。')).toBeVisible()
    await user.clear(input); await user.type(input, 'https://live.douyin.com/123456?foo=bar')
    await user.click(screen.getByRole('button', { name: '保存来源' }))
    expect(screen.getByRole('button', { name: '正在保存来源' })).toBeDisabled()
    resolveSave?.(json(source, { headers: { ETag: '"2"' } }))
    expect(await screen.findByText('已保存规范地址：https://live.douyin.com/123456')).toBeVisible()
    expect(mock).toHaveBeenCalledWith('/api/source', expect.objectContaining({ method: 'PUT' }))
  })

  it('ETag 冲突后刷新并提示重新检查', async () => {
    installApi({
      'PUT /api/source': json({ error: { code: 'write_conflict', message: 'changed', fields: {}, request_id: 'r' } }, { status: 409 }),
    })
    const user = userEvent.setup(); render(<App />)
    const input = await screen.findByLabelText('抖音直播间地址')
    await user.clear(input); await user.type(input, 'https://live.douyin.com/654321')
    await user.click(screen.getByRole('button', { name: '保存来源' }))
    expect(await screen.findByText('配置已被其他操作更新，已刷新，请重新检查后保存。')).toBeVisible()
  })

  it('来源冲突后的刷新失败只显示固定脱敏错误', async () => {
    let sourceReads = 0
    const baseFetch = installApi({
      'PUT /api/source': json({ error: { code: 'write_conflict', message: 'changed', fields: {}, request_id: 'r' } }, { status: 409 }),
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/source' && (init?.method ?? 'GET') === 'GET' && sourceReads++ > 0) {
        return json({ error: { code: 'refresh_failed', message: 'C:\\private\\source token=secret', fields: {}, request_id: 'r' } }, { status: 500 })
      }
      return baseFetch(input, init)
    }))
    const user = userEvent.setup(); render(<App />)
    const input = await screen.findByLabelText('抖音直播间地址')
    await user.clear(input); await user.type(input, 'https://live.douyin.com/654321')
    await user.click(screen.getByRole('button', { name: '保存来源' }))
    expect(await screen.findByText('配置冲突后刷新失败，请重试。')).toBeVisible()
    expect(screen.queryByText(/private|token=secret/)).not.toBeInTheDocument()
  })

  it('输出冲突后的刷新失败只显示固定脱敏错误', async () => {
    let destinationReads = 0
    const baseFetch = installApi({
      'PUT /api/destinations/douyin': json({ error: { code: 'write_conflict', message: 'changed', fields: {}, request_id: 'r' } }, { status: 412 }),
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/destinations/douyin' && (init?.method ?? 'GET') === 'GET' && destinationReads++ > 0) {
        return json({ error: { code: 'refresh_failed', message: 'stream_key=private-output-key', fields: {}, request_id: 'r' } }, { status: 500 })
      }
      return baseFetch(input, init)
    }))
    const user = userEvent.setup(); render(<App />)
    const card = await screen.findByRole('region', { name: '抖音输出' })
    await user.click(within(card).getByRole('button', { name: '保存输出' }))
    expect(await within(card).findByText('配置冲突后刷新失败，请重试。')).toBeVisible()
    expect(screen.queryByText(/private-output-key/)).not.toBeInTheDocument()
  })

  it('两个目标操作独立：单独重连不阻塞另一张卡', async () => {
    let resolveReconnect: ((value: Response) => void) | undefined
    installApi({ 'POST /api/destinations/douyin/reconnect': new Promise<Response>((resolve) => { resolveReconnect = resolve }) })
    const user = userEvent.setup(); render(<App />)
    const douyinCard = await screen.findByRole('region', { name: '抖音输出' })
    const wechatCard = screen.getByRole('region', { name: '微信视频号输出' })
    await user.click(within(douyinCard).getByRole('button', { name: '单独重连' }))
    expect(within(douyinCard).getByRole('button', { name: '正在重连' })).toBeDisabled()
    expect(within(wechatCard).getByRole('button', { name: '测试连接' })).toBeEnabled()
    resolveReconnect?.(json({ kind: 'douyin', status: 'reconnect_requested' }))
    expect(await within(douyinCard).findByText('已请求重连。')).toBeVisible()
  })

  it('API 错误显示重试并可恢复', async () => {
    let calls = 0
    installApi({ '/api/status': undefined })
    const original = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/status' && calls++ === 0) return json({ error: { code: 'offline', message: 'down', fields: {}, request_id: 'r' } }, { status: 503 })
      return original(input, init)
    }))
    const user = userEvent.setup(); render(<App />)
    expect(await screen.findByText('状态加载失败。')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '重试加载状态' }))
    expect(await screen.findByText('离线')).toBeVisible()
  })

  it.each([
    ['LIVE', '直播中'], ['RECONNECTING', '重连中'], ['AUTH_FAILED', '认证失败'], ['ERROR', '异常'], ['STANDBY', '待命'], ['OFFLINE', '离线'],
  ])('把 %s 状态显示为文字与图形', async (raw: string, label: string) => {
    installApi({ '/api/status': { ...status, source_state: raw, outputs: [{ ...status.outputs[0], status: raw }] } })
    render(<App />)
    const badges = await screen.findAllByText(label)
    expect(badges[0].previousElementSibling).toHaveAttribute('aria-hidden', 'true')
  })

  it('监控指标使用原生表格表头和数据单元格', async () => {
    installApi(); render(<App />)
    const table = await screen.findByRole('table', { name: '来源与输出实时指标' })
    expect(within(table).getAllByRole('columnheader')).toHaveLength(8)
    expect(within(table).getAllByRole('row')).toHaveLength(4)
    expect(within(table).getAllByRole('cell').length).toBeGreaterThanOrEqual(24)
  })

  it('开始与停止调用真实 API，只有活跃输出时停止需要可聚焦确认', async () => {
    const mock = installApi({
      '/api/status': { ...status, desired_running: true, outputs: [{ ...status.outputs[0], status: 'LIVE' }] },
      'POST /api/control/start': { status: 'started' },
      'POST /api/control/stop': { status: 'stopped' },
    })
    const user = userEvent.setup(); render(<App />)
    await user.click(await screen.findByRole('button', { name: '停止全部' }))
    const dialog = screen.getByRole('dialog', { name: '停止全部输出？' })
    expect(within(dialog).getByRole('button', { name: '确认停止' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '停止全部' })); await user.click(screen.getByRole('button', { name: '确认停止' }))
    expect(mock).toHaveBeenCalledWith('/api/control/stop', expect.objectContaining({ method: 'POST' }))
  })

  it('配置就绪时开始监控直接调用 API', async () => {
    const mock = installApi({ 'POST /api/control/start': { status: 'started' } })
    const user = userEvent.setup(); render(<App />)
    await user.click(await screen.findByRole('button', { name: '开始监控' }))
    expect(mock).toHaveBeenCalledWith('/api/control/start', expect.objectContaining({ method: 'POST' }))
    expect(await screen.findByText('已提交开始监控，等待后端确认状态。')).toBeVisible()
  })

  it('日志支持筛选、疑似密钥脱敏、加载更多与空状态', async () => {
    installApi({
      '/api/events?limit=50&cursor=0': { items: [
        { id: 1, created_at: '2026-01-01T00:00:00Z', level: 'ERROR', event_type: 'auth_failed', payload: { stream_key: 'super-secret', message: 'token=abcdef1234567890' } },
      ], next_cursor: 1 },
      '/api/events?limit=50&cursor=1': events,
    })
    const user = userEvent.setup(); render(<App />)
    expect(await screen.findByText('auth_failed')).toBeVisible()
    expect(screen.queryByText(/super-secret|abcdef1234567890/)).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('日志类别'), 'info')
    expect(screen.getByText('当前筛选下没有日志。')).toBeVisible()
    await user.selectOptions(screen.getByLabelText('日志类别'), 'all')
    await user.click(screen.getByRole('button', { name: '加载更多日志' }))
    expect(screen.getByText('没有更多日志。')).toBeVisible()
  })

  it('页面隐藏时停止轮询，再显示立即刷新，并取消旧请求避免过期响应覆盖', async () => {
    vi.useFakeTimers()
    const signals: AbortSignal[] = []
    let statusCalls = 0
    const mock = installApi()
    mock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/status') {
        signals.push(init?.signal as AbortSignal)
        statusCalls += 1
        if (statusCalls === 2) return new Promise<Response>(() => undefined)
      }
      const map: Record<string, unknown> = { '/api/session': { session_token: 't' }, '/api/source': source, '/api/destinations/douyin': destinations.douyin, '/api/destinations/wechat_channels': destinations.wechat_channels, '/api/status': status, '/api/events?limit=50&cursor=0': events }
      return json(map[path], path.includes('/source') || path.includes('/destinations/') ? { headers: { ETag: '"1"' } } : {})
    })
    const view = render(<App />); await screen.findByText('离线')
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(signals).toHaveLength(2)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' }); document.dispatchEvent(new Event('visibilitychange'))
    await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
    expect(signals).toHaveLength(2); expect(signals[1].aborted).toBe(true)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' }); document.dispatchEvent(new Event('visibilitychange'))
    await act(async () => { await Promise.resolve() }); expect(signals).toHaveLength(3)
    const latest = signals.at(-1); view.unmount(); expect(latest?.aborted).toBe(true)
  })

  it('初始化区块并行独立完成，来源失败不拖垮输出、状态和日志且可单独重试', async () => {
    let sourceCalls = 0
    const baseFetch = installApi()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/source' && sourceCalls++ === 0) {
        return json({ error: { code: 'source_down', message: 'private backend detail', fields: {}, request_id: 'r' } }, { status: 503 })
      }
      return baseFetch(input, init)
    }))
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByRole('region', { name: '抖音输出' })).toBeVisible()
    expect(screen.getByRole('region', { name: '运行监控' })).toBeVisible()
    expect(screen.getByRole('region', { name: '运行日志' })).toBeVisible()
    expect(screen.getByText('来源加载失败。')).toBeVisible()
    expect(screen.queryByText('private backend detail')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '重试加载来源' }))
    expect(await screen.findByRole('region', { name: '直播来源' })).toBeVisible()
  })

  it('一个初始化请求未完成时只保留该区块 loading', async () => {
    let resolveSource: ((value: Response) => void) | undefined
    installApi({ '/api/source': new Promise<Response>((resolve) => { resolveSource = resolve }) })
    render(<App />)

    expect(await screen.findByRole('region', { name: '抖音输出' })).toBeVisible()
    expect(screen.getByLabelText('正在加载来源')).toBeVisible()
    expect(screen.queryByLabelText('正在加载输出')).not.toBeInTheDocument()
    resolveSource?.(json(source, { headers: { ETag: '"1"' } }))
    expect(await screen.findByRole('region', { name: '直播来源' })).toBeVisible()
  })

  it('会话认证失败时禁用控制并可独立重试恢复', async () => {
    let sessionCalls = 0
    const baseFetch = installApi()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/session' && sessionCalls++ === 0) {
        return json({ error: { code: 'session_unavailable', message: 'token internals', fields: {}, request_id: 'r' } }, { status: 503 })
      }
      return baseFetch(input, init)
    }))
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByText('安全会话初始化失败。')).toBeVisible()
    expect(screen.getByRole('button', { name: '开始监控' })).toBeDisabled()
    expect(screen.queryByText('token internals')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试初始化会话' }))
    await waitFor(() => expect(screen.queryByText('安全会话初始化失败。')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: '开始监控' })).toBeEnabled()
  })

  it('不显示控制接口返回的后端错误 message', async () => {
    installApi({
      'POST /api/control/start': json({ error: { code: 'start_failed', message: 'C:\\private\\secret token=abc', fields: {}, request_id: 'r' } }, { status: 503 }),
    })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: '开始监控' }))
    expect(await screen.findByText('操作失败，请重试。')).toBeVisible()
    expect(screen.queryByText(/private|token=abc/)).not.toBeInTheDocument()
  })

  it('关闭停止确认对话框后恢复焦点到触发按钮', async () => {
    installApi({ '/api/status': { ...status, desired_running: true, outputs: [{ ...status.outputs[0], status: 'LIVE' }] } })
    const user = userEvent.setup()
    render(<App />)
    const stop = await screen.findByRole('button', { name: '停止全部' })
    await user.click(stop)
    await user.keyboard('{Escape}')
    expect(stop).toHaveFocus()
  })

  it('停止确认对话框循环焦点并隐藏主内容，关闭后恢复', async () => {
    installApi({ '/api/status': { ...status, desired_running: true, outputs: [{ ...status.outputs[0], status: 'LIVE' }] } })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: '停止全部' }))
    const main = screen.getByRole('main', { hidden: true })
    const dialog = screen.getByRole('dialog', { name: '停止全部输出？' })
    const cancel = within(dialog).getByRole('button', { name: '继续监控' })
    const confirm = within(dialog).getByRole('button', { name: '确认停止' })
    expect(main).toHaveAttribute('inert')
    expect(main).toHaveAttribute('aria-hidden', 'true')
    expect(confirm).toHaveFocus()
    await user.tab()
    expect(cancel).toHaveFocus()
    await user.tab({ shift: true })
    expect(confirm).toHaveFocus()
    await user.click(cancel)
    expect(screen.getByRole('main')).not.toHaveAttribute('inert')
    expect(screen.getByRole('main')).not.toHaveAttribute('aria-hidden')
  })

  it('可见时严格每 2 秒轮询一次状态', async () => {
    vi.useFakeTimers()
    const mock = installApi()
    render(<App />)
    await act(async () => { await Promise.resolve() })
    const countStatus = () => mock.mock.calls.filter(([input]) => input === '/api/status').length
    expect(countStatus()).toBe(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1999) })
    expect(countStatus()).toBe(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(countStatus()).toBe(2)
  })

  it('慢状态请求完成后才等待 2 秒再轮询且不会被定时器中止', async () => {
    vi.useFakeTimers()
    let resolveStatus: ((value: Response) => void) | undefined
    const signals: AbortSignal[] = []
    const baseFetch = installApi({ '/api/status': new Promise<Response>((resolve) => { resolveStatus = resolve }) })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/status') signals.push(init?.signal as AbortSignal)
      return baseFetch(input, init)
    }))
    render(<App />)
    await act(async () => { await Promise.resolve() })
    expect(signals).toHaveLength(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(false)
    resolveStatus?.(json(status))
    await act(async () => { await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(1999) })
    expect(signals).toHaveLength(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(signals).toHaveLength(2)
  })

  it('日志请求用 sequence 阻止忽略 abort 的旧响应覆盖新响应', async () => {
    let resolveOld: ((value: Response) => void) | undefined
    installApi({
      '/api/events?limit=50&cursor=0': { items: [], next_cursor: 1 },
      '/api/events?limit=50&cursor=1': new Promise<Response>((resolve) => { resolveOld = resolve }),
    })
    const user = userEvent.setup()
    render(<App />)
    await screen.findByRole('region', { name: '运行日志' })
    await user.click(screen.getByRole('button', { name: '加载更多日志' }))
    await user.click(screen.getByRole('button', { name: '刷新日志' }))
    resolveOld?.(json({ items: [{ id: 99, created_at: '2026-01-01T00:00:00Z', level: 'ERROR', event_type: 'stale_event', payload: {} }], next_cursor: null }))
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByText('stale_event')).not.toBeInTheDocument()
  })
})
