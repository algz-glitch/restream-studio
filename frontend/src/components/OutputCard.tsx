import { useEffect, useRef, useState } from 'react'
import { ApiError, isAbortError, type ApiClient } from '../api'
import type { DestinationKind, DestinationResponse } from '../types'
import { StatusBadge } from './StatusBadge'

const names: Record<DestinationKind, string> = { douyin: '抖音输出', wechat_channels: '微信视频号输出' }
interface Props { destination: DestinationResponse; api: ApiClient; onChange: (value: DestinationResponse) => void }

export function OutputCard({ destination, api, onChange }: Props) {
  const [server, setServer] = useState('')
  const [key, setKey] = useState('')
  const [enabled, setEnabled] = useState(destination.enabled)
  const [busy, setBusy] = useState<'save' | 'test' | 'reconnect' | null>(null)
  const [feedback, setFeedback] = useState('')
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => setEnabled(destination.enabled), [destination.enabled])
  useEffect(() => () => request.current?.abort(), [])

  async function act(type: 'save' | 'test' | 'reconnect') {
    setBusy(type); setError(''); setFeedback('')
    request.current?.abort(); request.current = new AbortController()
    try {
      if (type === 'save') {
        if (!destination.configured && (!/^rtmps?:\/\//.test(server) || key.length < 8)) { setError('请填写有效的 RTMP(S) 服务器和至少 8 位推流密钥。'); return }
        const payload = { enabled, ...(server ? { base_server: server } : {}), ...(key ? { stream_key: key } : {}) }
        const value = await api.saveDestination(destination.kind, payload, request.current.signal); onChange(value); setKey(''); setServer(''); setFeedback('输出配置已保存。')
      } else if (type === 'test') {
        const value = await api.testDestination(destination.kind, request.current.signal); setFeedback(value.ok ? '连接测试通过。' : '连接测试失败。')
      } else { await api.reconnect(destination.kind, request.current.signal); setFeedback('已请求重连。') }
    } catch (cause) {
      if (isAbortError(cause)) return
      if (cause instanceof ApiError && [409, 412].includes(cause.status) && type === 'save') {
        const fresh = await api.getDestination(destination.kind, request.current.signal); onChange(fresh.data); setFeedback('配置已更新，已刷新，请重新检查。')
      } else setError(type === 'save' ? '保存失败，请重试。' : '操作失败，请重试。')
    } finally { setBusy(null) }
  }

  return <section className="panel output" role="region" aria-labelledby={`${destination.kind}-title`}>
    <div className="panel__heading"><div><h2 id={`${destination.kind}-title`}>{names[destination.kind]}</h2><p>{destination.kind === 'douyin' ? 'RTMP 发布通道 A' : 'RTMP 发布通道 B'}</p></div><StatusBadge state={destination.status} /></div>
    <div className="stack">
      <label>推流服务器<input value={server} onChange={(e) => setServer(e.target.value)} placeholder={destination.configured ? '已保存；留空保持不变' : 'rtmps://example.com/live'} /></label>
      <label>推流密钥<input type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="输入新密钥" />{destination.masked_stream_key && <span className="helper">已保存：********</span>}</label>
      <label className="switch-row"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /><span>启用此输出</span></label>
    </div>
    <div className="button-row"><button className="button button--primary" disabled={busy !== null} onClick={() => void act('save')}>{busy === 'save' ? '正在保存' : '保存输出'}</button><button className="button" disabled={busy !== null || !destination.configured} onClick={() => void act('test')}>{busy === 'test' ? '正在测试' : '测试连接'}</button><button className="button" disabled={busy !== null || !destination.configured} onClick={() => void act('reconnect')}>{busy === 'reconnect' ? '正在重连' : '单独重连'}</button></div>
    <div className={error ? 'feedback feedback--error' : 'feedback'} aria-live="polite">{error || feedback || (!destination.configured ? '下一步：保存服务器和推流密钥。' : '密钥不会被回显或写入日志。')}</div>
  </section>
}
