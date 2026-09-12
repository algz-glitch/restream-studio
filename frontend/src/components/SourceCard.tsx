import { useEffect, useRef, useState } from 'react'
import { ApiError, type ApiClient } from '../api'
import type { SourceResponse } from '../types'

interface Props { source: SourceResponse; api: ApiClient; onChange: (value: SourceResponse) => void }

export function SourceCard({ source, api, onChange }: Props) {
  const [url, setUrl] = useState(source.room_identity ?? '')
  const [quality, setQuality] = useState(source.preferred_quality ?? 'origin')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => { setUrl(source.room_identity ?? ''); setQuality(source.preferred_quality ?? 'origin') }, [source])
  useEffect(() => () => request.current?.abort(), [])

  async function save() {
    setError(''); setNotice('')
    let parsed: URL
    try { parsed = new URL(url) } catch { setError('请输入有效的直播间地址。'); return }
    const segment = parsed.pathname.replace(/^\//, '').replace(/\/$/, '')
    const validLive = parsed.hostname === 'live.douyin.com' && /^[A-Za-z0-9_-]+$/.test(segment)
    const validShort = parsed.hostname === 'v.douyin.com' && /^[A-Za-z0-9]+$/.test(segment)
    if (parsed.protocol !== 'https:' || (!validLive && !validShort) || parsed.hash) {
      setError('请输入支持的抖音直播间地址。'); return
    }
    setBusy(true)
    request.current?.abort(); request.current = new AbortController()
    try {
      const value = await api.saveSource({ room_url: url, preferred_quality: quality || null }, request.current.signal)
      onChange(value); setNotice(`已保存规范地址：${value.room_identity ?? ''}`)
    } catch (cause) {
      if (cause instanceof ApiError && [409, 412].includes(cause.status)) {
        const fresh = await api.getSource(request.current.signal); onChange(fresh.data); setNotice('配置已被其他操作更新，已刷新，请重新检查后保存。')
      } else if (cause instanceof ApiError && cause.fields.room_url) setError('直播间地址未通过校验。')
      else setError('来源保存失败，请重试。')
    } finally { setBusy(false) }
  }

  return <section className="panel source" role="region" aria-labelledby="source-title">
    <div className="panel__heading"><div><h2 id="source-title">直播来源</h2><p>授权的抖音直播间</p></div><span className="panel__index">INPUT 01</span></div>
    <div className="form-grid form-grid--source">
      <label>抖音直播间地址<input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://live.douyin.com/123456" aria-describedby="source-error" /></label>
      <label>清晰度<select value={quality} onChange={(event) => setQuality(event.target.value)}><option value="origin">原画优先</option><option value="uhd">超清</option><option value="hd">高清</option><option value="sd">标清</option></select></label>
      <button className="button button--primary align-end" onClick={() => void save()} disabled={busy}>{busy ? '正在保存来源' : '保存来源'}</button>
    </div>
    <div id="source-error" className={error ? 'feedback feedback--error' : 'feedback'} aria-live="polite">{error || notice || (source.configured ? `当前规范地址：${source.room_identity}` : '下一步：填写直播间地址并保存。')}</div>
  </section>
}
