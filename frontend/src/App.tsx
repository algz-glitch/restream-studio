import { useCallback, useEffect, useRef, useState } from 'react'
import { api as defaultApi, isAbortError, type ApiClient } from './api'
import type { DestinationKind, DestinationResponse, EventItem, SourceResponse, StatusResponse } from './types'
import { SourceCard } from './components/SourceCard'
import { OutputCard } from './components/OutputCard'
import { Monitor } from './components/Monitor'
import { Logs } from './components/Logs'
import { StatusBadge } from './components/StatusBadge'
import './styles.css'

const kinds = ['douyin', 'wechat_channels'] as const
const outputNames: Record<DestinationKind, string> = { douyin: '抖音输出', wechat_channels: '微信视频号输出' }
const blankStatus: StatusResponse = { desired_running: false, source_state: 'OFFLINE', source_failure: null, outputs: [] }

export default function App({ api = defaultApi }: { api?: ApiClient }) {
  const [source, setSource] = useState<SourceResponse | null>(null)
  const [sourceLoading, setSourceLoading] = useState(true)
  const [sourceError, setSourceError] = useState('')
  const [destinations, setDestinations] = useState<Partial<Record<DestinationKind, DestinationResponse>>>({})
  const [destinationLoading, setDestinationLoading] = useState<Record<DestinationKind, boolean>>({ douyin: true, wechat_channels: true })
  const [destinationErrors, setDestinationErrors] = useState<Partial<Record<DestinationKind, string>>>({})
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [statusLoading, setStatusLoading] = useState(true)
  const [statusError, setStatusError] = useState('')
  const [events, setEvents] = useState<EventItem[]>([])
  const [nextCursor, setNextCursor] = useState<number | null>(0)
  const [logsLoading, setLogsLoading] = useState(true)
  const [logsError, setLogsError] = useState('')
  const [sessionReady, setSessionReady] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(true)
  const [sessionError, setSessionError] = useState('')
  const [controlBusy, setControlBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [confirmStop, setConfirmStop] = useState(false)

  const sourceController = useRef<AbortController | null>(null)
  const destinationControllers = useRef<Partial<Record<DestinationKind, AbortController>>>({})
  const statusController = useRef<AbortController | null>(null)
  const statusRequest = useRef<Promise<void> | null>(null)
  const eventsController = useRef<AbortController | null>(null)
  const sessionController = useRef<AbortController | null>(null)
  const controlController = useRef<AbortController | null>(null)
  const sourceSequence = useRef(0)
  const destinationSequences = useRef<Record<DestinationKind, number>>({ douyin: 0, wechat_channels: 0 })
  const statusSequence = useRef(0)
  const eventsSequence = useRef(0)
  const sessionSequence = useRef(0)
  const confirmButton = useRef<HTMLButtonElement>(null)
  const stopButton = useRef<HTMLButtonElement>(null)
  const dialogWasOpen = useRef(false)
  const dialog = useRef<HTMLDivElement>(null)

  const loadSource = useCallback(async () => {
    sourceController.current?.abort()
    const controller = new AbortController()
    const sequence = ++sourceSequence.current
    sourceController.current = controller
    setSourceLoading(true)
    setSourceError('')
    try {
      const value = await api.getSource(controller.signal)
      if (!controller.signal.aborted && sequence === sourceSequence.current) setSource(value.data)
    } catch (cause) {
      if (!isAbortError(cause) && sequence === sourceSequence.current) setSourceError('来源加载失败。')
    } finally {
      if (sequence === sourceSequence.current) setSourceLoading(false)
    }
  }, [api])

  const loadDestination = useCallback(async (kind: DestinationKind) => {
    destinationControllers.current[kind]?.abort()
    const controller = new AbortController()
    const sequence = ++destinationSequences.current[kind]
    destinationControllers.current[kind] = controller
    setDestinationLoading((old) => ({ ...old, [kind]: true }))
    setDestinationErrors((old) => ({ ...old, [kind]: '' }))
    try {
      const value = await api.getDestination(kind, controller.signal)
      if (!controller.signal.aborted && sequence === destinationSequences.current[kind]) {
        setDestinations((old) => ({ ...old, [kind]: value.data }))
      }
    } catch (cause) {
      if (!isAbortError(cause) && sequence === destinationSequences.current[kind]) {
        setDestinationErrors((old) => ({ ...old, [kind]: '输出配置加载失败。' }))
      }
    } finally {
      if (sequence === destinationSequences.current[kind]) {
        setDestinationLoading((old) => ({ ...old, [kind]: false }))
      }
    }
  }, [api])

  const loadStatus = useCallback((): Promise<void> => {
    if (statusRequest.current && statusController.current && !statusController.current.signal.aborted) {
      return statusRequest.current
    }
    const controller = new AbortController()
    const sequence = ++statusSequence.current
    statusController.current = controller
    setStatusLoading(true)
    const pending = (async () => {
      try {
        const value = await api.getStatus(controller.signal)
        if (!controller.signal.aborted && sequence === statusSequence.current) {
          setStatus(value)
          setStatusError('')
        }
      } catch (cause) {
        if (!isAbortError(cause) && sequence === statusSequence.current) setStatusError('状态加载失败。')
      } finally {
        if (sequence === statusSequence.current) setStatusLoading(false)
        if (statusController.current === controller) {
          statusController.current = null
          statusRequest.current = null
        }
      }
    })()
    statusRequest.current = pending
    return pending
  }, [api])

  const loadEvents = useCallback(async (cursor = 0, append = false) => {
    eventsController.current?.abort()
    const controller = new AbortController()
    const sequence = ++eventsSequence.current
    eventsController.current = controller
    setLogsLoading(true)
    try {
      const page = await api.getEvents(cursor, controller.signal)
      if (!controller.signal.aborted && sequence === eventsSequence.current) {
        setEvents((old) => append ? [...old, ...page.items.filter((item) => !old.some((existing) => existing.id === item.id))] : page.items)
        setNextCursor(page.next_cursor)
        setLogsError('')
      }
    } catch (cause) {
      if (!isAbortError(cause) && sequence === eventsSequence.current) setLogsError('日志加载失败。')
    } finally {
      if (sequence === eventsSequence.current) setLogsLoading(false)
    }
  }, [api])

  const loadSession = useCallback(async () => {
    sessionController.current?.abort()
    const controller = new AbortController()
    const sequence = ++sessionSequence.current
    sessionController.current = controller
    setSessionLoading(true)
    setSessionError('')
    try {
      await api.session(controller.signal)
      if (!controller.signal.aborted && sequence === sessionSequence.current) setSessionReady(true)
    } catch (cause) {
      if (!isAbortError(cause) && sequence === sessionSequence.current) {
        setSessionReady(false)
        setSessionError('安全会话初始化失败。')
      }
    } finally {
      if (sequence === sessionSequence.current) setSessionLoading(false)
    }
  }, [api])

  useEffect(() => {
    void loadSource()
    for (const kind of kinds) void loadDestination(kind)
    void loadEvents()
    void loadSession()
    return () => {
      sourceController.current?.abort()
      for (const controller of Object.values(destinationControllers.current)) controller?.abort()
      statusController.current?.abort()
      eventsController.current?.abort()
      sessionController.current?.abort()
      controlController.current?.abort()
    }
  }, [loadDestination, loadEvents, loadSession, loadSource])

  useEffect(() => {
    let timer: number | undefined
    let generation = 0
    let disposed = false
    function clearTimer() {
      if (timer !== undefined) window.clearTimeout(timer)
      timer = undefined
    }
    async function poll(expectedGeneration: number) {
      await loadStatus()
      if (disposed || expectedGeneration !== generation || document.visibilityState !== 'visible') return
      timer = window.setTimeout(() => void poll(expectedGeneration), 2000)
    }
    function stop() {
      generation += 1
      clearTimer()
      statusController.current?.abort()
      statusController.current = null
      statusRequest.current = null
    }
    function start() {
      clearTimer()
      const expectedGeneration = ++generation
      void poll(expectedGeneration)
    }
    function visibility() {
      if (document.visibilityState === 'hidden') stop()
      else start()
    }
    if (document.visibilityState === 'visible') start()
    document.addEventListener('visibilitychange', visibility)
    return () => {
      disposed = true
      stop()
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [loadStatus])

  useEffect(() => {
    if (confirmStop) {
      dialogWasOpen.current = true
      confirmButton.current?.focus()
    } else if (dialogWasOpen.current) {
      dialogWasOpen.current = false
      stopButton.current?.focus()
    }
  }, [confirmStop])

  useEffect(() => {
    if (!confirmStop) return
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setConfirmStop(false)
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) {
        event.preventDefault(); first.focus()
      }
    }
    window.addEventListener('keydown', keyboard)
    return () => window.removeEventListener('keydown', keyboard)
  }, [confirmStop])

  const configuredOutput = (Object.values(destinations) as DestinationResponse[]).some((item) => item.configured && item.enabled)
  const canStart = Boolean(sessionReady && source?.configured && configuredOutput && !controlBusy && !status?.desired_running)
  const activeOutputs = status?.outputs.some((item) => ['LIVE', 'RUNNING', 'RECONNECTING', 'CONNECTING'].includes(item.status.toUpperCase())) ?? false

  async function control(action: 'start' | 'stop') {
    if (action === 'stop' && activeOutputs && !confirmStop) { setConfirmStop(true); return }
    setConfirmStop(false)
    setControlBusy(true)
    setNotice('')
    controlController.current?.abort()
    const controller = new AbortController()
    controlController.current = controller
    try {
      if (action === 'start') await api.start(controller.signal)
      else await api.stop(controller.signal)
      setNotice(action === 'start' ? '已提交开始监控，等待后端确认状态。' : '已停止全部输出。')
      await loadStatus()
    } catch (cause) {
      if (!isAbortError(cause)) setNotice('操作失败，请重试。')
    } finally {
      setControlBusy(false)
    }
  }

  const outputStates = status?.outputs.map((item) => item.status.toUpperCase()) ?? []
  const globalState = outputStates.some((state) => ['LIVE', 'RUNNING'].includes(state)) ? 'LIVE'
    : outputStates.some((state) => ['RECONNECTING', 'CONNECTING'].includes(state)) ? 'RECONNECTING'
      : outputStates.includes('AUTH_FAILED') ? 'AUTH_FAILED'
        : outputStates.includes('ERROR') ? 'ERROR'
          : status?.desired_running ? 'STANDBY' : 'OFFLINE'
  const initializing = sourceLoading || statusLoading || logsLoading || sessionLoading || kinds.some((kind) => destinationLoading[kind])

  return <div className="app-shell">
    <div className="app-background" data-testid="app-background" aria-hidden={confirmStop || undefined} inert={confirmStop || undefined}>
      <header className="command-bar">
      <div className="brand"><span className="brand__mark" aria-hidden="true">RS</span><div><strong>Restream Studio</strong><small>本地双路播控台</small></div></div>
      <StatusBadge state={globalState} />
      <div className="command-actions"><button className="button button--primary" disabled={!canStart} aria-describedby="start-help" onClick={() => void control('start')}>{controlBusy ? '正在处理' : '开始监控'}</button><button ref={stopButton} className="button" disabled={!sessionReady || controlBusy || !status?.desired_running} onClick={() => void control('stop')}>停止全部</button></div>
      </header>
      <main aria-busy={initializing}>
      <div className="intro"><div><h1>传输控制台</h1><p>配置一次来源，分别监管两个发布通道。</p></div><div id="start-help" className="start-help">{!sessionReady ? '正在建立本地安全会话。' : !source?.configured || !configuredOutput ? '先保存抖音直播间地址，再配置并启用至少一个输出目标。' : '配置就绪，可以开始监控。'}</div></div>
      <div className="sr-live" aria-live="polite">{notice}</div>
      {sessionError && <LoadError text={sessionError} retryLabel="重试初始化会话" onRetry={() => void loadSession()} />}
      {sourceLoading ? <PanelSkeleton label="正在加载来源" /> : sourceError || !source ? <LoadError text={sourceError || '来源加载失败。'} retryLabel="重试加载来源" onRetry={() => void loadSource()} /> : <SourceCard source={source} api={api} onChange={setSource} />}
      <div className="output-grid">{kinds.map((kind) => destinationLoading[kind]
        ? <PanelSkeleton key={kind} label="正在加载输出" tall />
        : destinationErrors[kind] || !destinations[kind]
          ? <LoadError key={kind} text={destinationErrors[kind] || '输出配置加载失败。'} retryLabel={`重试加载${outputNames[kind]}`} onRetry={() => void loadDestination(kind)} />
          : <OutputCard key={kind} destination={destinations[kind]} api={api} onChange={(value) => setDestinations((old) => ({ ...old, [kind]: value }))} />)}</div>
      <div className="lower-grid">
        {statusLoading && !status ? <PanelSkeleton label="正在加载状态" /> : statusError ? <LoadError text={statusError} retryLabel="重试加载状态" onRetry={() => void loadStatus()} /> : <Monitor status={status ?? blankStatus} />}
        {logsLoading && events.length === 0 && nextCursor === 0 && !logsError ? <PanelSkeleton label="正在加载日志" /> : <Logs items={events} loading={logsLoading} error={logsError} hasMore={nextCursor !== null} onMore={() => void loadEvents(nextCursor ?? 0, true)} onRetry={() => void loadEvents(0)} />}
      </div>
      </main>
    </div>
    {confirmStop && <div className="dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setConfirmStop(false) }}><div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="stop-title" aria-describedby="stop-description" className="dialog"><h2 id="stop-title">停止全部输出？</h2><p id="stop-description">当前有活跃输出。确认后两路发布都会停止。</p><div className="button-row"><button className="button" onClick={() => setConfirmStop(false)}>继续监控</button><button ref={confirmButton} className="button button--danger" onClick={() => void control('stop')}>确认停止</button></div></div></div>}
  </div>
}

function PanelSkeleton({ label, tall = false }: { label: string; tall?: boolean }) {
  return <div className={`panel skeleton${tall ? ' skeleton--tall' : ''}`} aria-label={label} data-testid="skeleton" />
}

function LoadError({ text, retryLabel, onRetry }: { text: string; retryLabel: string; onRetry: () => void }) {
  return <section className="panel error-panel"><p>{text}</p><button className="button" onClick={onRetry}>{retryLabel}</button></section>
}
