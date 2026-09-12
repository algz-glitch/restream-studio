import { useCallback, useEffect, useRef, useState } from 'react'
import { api as defaultApi, ApiError, type ApiClient } from './api'
import type { DestinationKind, DestinationResponse, EventItem, SourceResponse, StatusResponse } from './types'
import { SourceCard } from './components/SourceCard'
import { OutputCard } from './components/OutputCard'
import { Monitor } from './components/Monitor'
import { Logs } from './components/Logs'
import { StatusBadge } from './components/StatusBadge'
import './styles.css'

const blankStatus: StatusResponse = { desired_running: false, source_state: 'OFFLINE', source_failure: null, outputs: [] }

export default function App({ api = defaultApi }: { api?: ApiClient }) {
  const [source, setSource] = useState<SourceResponse | null>(null)
  const [destinations, setDestinations] = useState<Partial<Record<DestinationKind, DestinationResponse>>>({})
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [nextCursor, setNextCursor] = useState<number | null>(0)
  const [initialLoading, setInitialLoading] = useState(true)
  const [statusError, setStatusError] = useState('')
  const [logsError, setLogsError] = useState('')
  const [logsLoading, setLogsLoading] = useState(false)
  const [controlBusy, setControlBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [confirmStop, setConfirmStop] = useState(false)
  const statusController = useRef<AbortController | null>(null)
  const eventsController = useRef<AbortController | null>(null)
  const controlController = useRef<AbortController | null>(null)
  const statusSequence = useRef(0)
  const confirmButton = useRef<HTMLButtonElement>(null)

  const loadStatus = useCallback(async () => {
    statusController.current?.abort()
    const controller = new AbortController(); statusController.current = controller
    const sequence = ++statusSequence.current
    try {
      const value = await api.getStatus(controller.signal)
      if (!controller.signal.aborted && sequence === statusSequence.current) { setStatus(value); setStatusError('') }
    } catch (cause) { if (!(cause instanceof DOMException && cause.name === 'AbortError') && sequence === statusSequence.current) setStatusError('状态加载失败。') }
  }, [api])

  const loadEvents = useCallback(async (cursor = 0, append = false) => {
    eventsController.current?.abort(); const controller = new AbortController(); eventsController.current = controller
    setLogsLoading(true)
    try { const page = await api.getEvents(cursor, controller.signal); if (!controller.signal.aborted) { setEvents((old) => append ? [...old, ...page.items.filter((item) => !old.some((existing) => existing.id === item.id))] : page.items); setNextCursor(page.next_cursor); setLogsError('') } }
    catch (cause) { if (!(cause instanceof DOMException && cause.name === 'AbortError')) setLogsError('日志加载失败。') } finally { if (!controller.signal.aborted) setLogsLoading(false) }
  }, [api])

  useEffect(() => {
    const controller = new AbortController()
    async function initialize() {
      try {
        const [sourceValue, douyin, wechat, statusValue, eventValue] = await Promise.all([
          api.getSource(controller.signal), api.getDestination('douyin', controller.signal), api.getDestination('wechat_channels', controller.signal), api.getStatus(controller.signal), api.getEvents(0, controller.signal), api.session(controller.signal),
        ])
        if (controller.signal.aborted) return
        setSource(sourceValue.data); setDestinations({ douyin: douyin.data, wechat_channels: wechat.data }); setStatus(statusValue); setEvents(eventValue.items); setNextCursor(eventValue.next_cursor)
      } catch (cause) { if (!(cause instanceof DOMException && cause.name === 'AbortError')) { setStatusError('状态加载失败。'); setLogsError('日志加载失败。') } }
      finally { if (!controller.signal.aborted) setInitialLoading(false) }
    }
    void initialize()
    return () => { controller.abort(); statusController.current?.abort(); eventsController.current?.abort(); controlController.current?.abort() }
  }, [api])

  useEffect(() => {
    let timer: number | undefined
    function schedule() { if (document.visibilityState === 'visible') timer = window.setInterval(() => void loadStatus(), 2000) }
    function visibility() { if (timer) window.clearInterval(timer); statusController.current?.abort(); if (document.visibilityState === 'visible') { void loadStatus(); schedule() } }
    schedule(); document.addEventListener('visibilitychange', visibility)
    return () => { if (timer) window.clearInterval(timer); document.removeEventListener('visibilitychange', visibility); statusController.current?.abort() }
  }, [loadStatus])

  useEffect(() => { if (confirmStop) confirmButton.current?.focus() }, [confirmStop])
  useEffect(() => { if (!confirmStop) return; const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setConfirmStop(false) }; window.addEventListener('keydown', escape); return () => window.removeEventListener('keydown', escape) }, [confirmStop])

  const configuredOutput = (Object.values(destinations) as DestinationResponse[]).some((item) => item.configured && item.enabled)
  const canStart = Boolean(source?.configured && configuredOutput && !controlBusy && !status?.desired_running)
  const activeOutputs = status?.outputs.some((item) => ['LIVE', 'RUNNING', 'RECONNECTING', 'CONNECTING'].includes(item.status.toUpperCase())) ?? false

  async function control(action: 'start' | 'stop') {
    if (action === 'stop' && activeOutputs && !confirmStop) { setConfirmStop(true); return }
    setConfirmStop(false); setControlBusy(true); setNotice('')
    controlController.current?.abort(); controlController.current = new AbortController()
    try { action === 'start' ? await api.start(controlController.current.signal) : await api.stop(controlController.current.signal); setNotice(action === 'start' ? '已提交开始监控，等待后端确认状态。' : '已停止全部输出。'); await loadStatus() }
    catch (cause) { setNotice(cause instanceof ApiError ? `操作失败：${cause.message}` : '操作失败，请重试。') }
    finally { setControlBusy(false) }
  }

  const outputStates = status?.outputs.map((item) => item.status.toUpperCase()) ?? []
  const globalState = outputStates.some((state) => ['LIVE', 'RUNNING'].includes(state)) ? 'LIVE'
    : outputStates.some((state) => ['RECONNECTING', 'CONNECTING'].includes(state)) ? 'RECONNECTING'
      : outputStates.includes('AUTH_FAILED') ? 'AUTH_FAILED'
        : outputStates.includes('ERROR') ? 'ERROR'
          : status?.desired_running ? 'STANDBY' : 'OFFLINE'
  return <div className="app-shell">
    <header className="command-bar"><div className="brand"><span className="brand__mark" aria-hidden="true">RS</span><div><strong>Restream Studio</strong><small>本地双路播控台</small></div></div><StatusBadge state={globalState} /><div className="command-actions"><button className="button button--primary" disabled={!canStart} aria-describedby="start-help" onClick={() => void control('start')}>{controlBusy ? '正在处理' : '开始监控'}</button><button className="button" disabled={controlBusy || !status?.desired_running} onClick={() => void control('stop')}>停止全部</button></div></header>
    <main aria-busy={initialLoading}>
      <div className="intro"><div><h1>传输控制台</h1><p>配置一次来源，分别监管两个发布通道。</p></div><div id="start-help" className="start-help">{!source?.configured || !configuredOutput ? '先保存抖音直播间地址，再配置并启用至少一个输出目标。' : '配置就绪，可以开始监控。'}</div></div>
      <div className="sr-live" aria-live="polite">{notice}</div>
      {initialLoading ? <Skeleton /> : <>
        {source ? <SourceCard source={source} api={api} onChange={setSource} /> : <LoadError text="来源加载失败。" />}
        <div className="output-grid">{(['douyin', 'wechat_channels'] as const).map((kind) => destinations[kind] ? <OutputCard key={kind} destination={destinations[kind]} api={api} onChange={(value) => setDestinations((old) => ({ ...old, [kind]: value }))} /> : <LoadError key={kind} text="输出配置加载失败。" />)}</div>
        <div className="lower-grid">{statusError ? <section className="panel error-panel"><p>{statusError}</p><button className="button" onClick={() => void loadStatus()}>重试加载状态</button></section> : <Monitor status={status ?? blankStatus} />}<Logs items={events} loading={logsLoading} error={logsError} hasMore={nextCursor !== null} onMore={() => void loadEvents(nextCursor ?? 0, true)} onRetry={() => void loadEvents(0)} /></div>
      </>}
    </main>
    {confirmStop && <div className="dialog-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setConfirmStop(false) }}><div role="dialog" aria-modal="true" aria-labelledby="stop-title" className="dialog"><h2 id="stop-title">停止全部输出？</h2><p>当前有活跃输出。确认后两路发布都会停止。</p><div className="button-row"><button className="button" onClick={() => setConfirmStop(false)}>继续监控</button><button ref={confirmButton} className="button button--danger" onClick={() => void control('stop')}>确认停止</button></div></div></div>}
  </div>
}

function Skeleton() { return <div className="skeleton-layout" aria-label="正在加载控制台"><div className="panel skeleton" data-testid="skeleton" /><div className="output-grid"><div className="panel skeleton skeleton--tall" data-testid="skeleton" /><div className="panel skeleton skeleton--tall" data-testid="skeleton" /></div><div className="panel skeleton" data-testid="skeleton" /></div> }
function LoadError({ text }: { text: string }) { return <section className="panel error-panel"><p>{text}</p><span>请刷新页面重试。</span></section> }
