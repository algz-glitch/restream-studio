import { useEffect, useRef, useState } from 'react'
import { isAbortError, type ApiClient } from '../api'
import type { UpdateResponse } from '../types'

const statusCopy: Record<UpdateResponse['status'], string> = {
  idle: '尚未检查更新',
  checking: '正在检查更新',
  current: '已是最新版本',
  available: '发现新版本',
  downloading: '正在下载更新',
  ready: '更新已准备就绪',
  failed: '更新操作失败，请稍后重试。',
}

const statusBadge: Record<UpdateResponse['status'], string> = {
  idle: '未检查', checking: '检查中', current: '最新版', available: '有更新',
  downloading: '下载中', ready: '待安装', failed: '失败',
}

type UpdateAction = 'check' | 'download' | 'install'

interface UpdateCardProps {
  update: UpdateResponse
  desiredRunning: boolean
  api: ApiClient
  onChange: (value: UpdateResponse) => void
  loadError?: string
  loading?: boolean
}

function approvedReleaseUrl(value: string | null): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:'
      && url.hostname === 'github.com'
      && url.pathname.startsWith('/algz-glitch/restream-studio/releases/')
      ? url.href : null
  } catch {
    return null
  }
}

export function UpdateCard({ update, desiredRunning, api, onChange, loadError = '', loading = false }: UpdateCardProps) {
  const [busy, setBusy] = useState<UpdateAction | null>(null)
  const [actionError, setActionError] = useState('')
  const [installNotice, setInstallNotice] = useState('')
  const [installScheduled, setInstallScheduled] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const actionLock = useRef(false)
  const installLock = useRef(false)

  useEffect(() => () => controller.current?.abort(), [])

  async function act(action: UpdateAction) {
    if (actionLock.current || (action === 'install' && (desiredRunning || installLock.current))) return
    actionLock.current = true
    controller.current?.abort()
    const nextController = new AbortController()
    controller.current = nextController
    setBusy(action)
    setActionError('')
    setInstallNotice('')
    try {
      if (action === 'check') onChange(await api.checkUpdate(nextController.signal))
      else if (action === 'download') onChange(await api.downloadUpdate(nextController.signal))
      else {
        await api.installUpdate(nextController.signal)
        installLock.current = true
        setInstallScheduled(true)
        setInstallNotice('已提交安装，软件将自动重启。')
      }
    } catch (cause) {
      if (!isAbortError(cause)) setActionError('更新操作失败，请稍后重试。')
    } finally {
      if (controller.current === nextController) controller.current = null
      actionLock.current = false
      setBusy(null)
    }
  }

  const releaseUrl = update.status === 'available' ? approvedReleaseUrl(update.release_url) : null
  const diagnostic = actionError || loadError || statusCopy[update.status]
  const canCheck = ['idle', 'current', 'failed'].includes(update.status)
  const canDownload = update.status === 'available'
  const canInstall = update.status === 'ready'

  return <section className="panel update-card" role="region" aria-label="软件更新" aria-busy={loading || busy !== null}>
    <div className="panel__heading update-card__heading">
      <div><span className="panel__index">SYSTEM / UPDATE</span><h2>软件更新</h2><p>安全获取并安装 Restream Studio 新版本。</p></div>
      <span className={`update-state update-state--${update.status}`}>{statusBadge[update.status]}</span>
    </div>
    <div className="update-card__body">
      <div className="update-version"><span>当前版本 {update.current_version}</span>{update.available_version && <strong>目标版本 {update.available_version}</strong>}</div>
      <p className={`feedback${actionError || loadError || update.status === 'failed' ? ' feedback--error' : ''}`} aria-live="polite">{diagnostic}</p>
      {releaseUrl && <a className="release-link" href={releaseUrl} target="_blank" rel="noopener noreferrer">查看 GitHub 发布说明</a>}
      {canInstall && desiredRunning && <p className="update-stop-hint" id="update-stop-hint">请先停止全部输出，再安装更新。</p>}
      {installNotice && <p className="feedback" aria-live="polite">{installNotice}</p>}
    </div>
    <div className="update-card__action">
      {canCheck && <button className="button button--primary" disabled={busy !== null || loading} onClick={() => void act('check')}>{busy === 'check' ? '正在检查' : '检查更新'}</button>}
      {canDownload && <button className="button button--primary" disabled={busy !== null || loading} onClick={() => void act('download')}>{busy === 'download' ? '正在下载' : '下载更新'}</button>}
      {canInstall && <button className="button button--primary" disabled={busy !== null || desiredRunning || loading || installScheduled} aria-describedby={desiredRunning ? 'update-stop-hint' : undefined} onClick={() => void act('install')}>{installScheduled ? '重启已安排' : busy === 'install' ? '正在安装' : '安装并重启'}</button>}
      {(update.status === 'checking' || update.status === 'downloading') && <button className="button button--primary" disabled>{update.status === 'checking' ? '正在检查' : '正在下载'}</button>}
    </div>
  </section>
}
