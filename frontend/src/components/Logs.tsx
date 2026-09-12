import { useMemo, useState } from 'react'
import type { EventItem } from '../types'

function redactValue(key: string, value: unknown): unknown {
  if (/key|token|secret|cookie|authorization|password/i.test(key)) return '***'
  if (typeof value === 'string') return value.replace(/(token|key|secret|password)=([^\s&,]+)/gi, '$1=***').replace(/rtmps?:\/\/[^\s"']+/gi, '[REDACTED_URL]')
  if (Array.isArray(value)) return value.map((item) => redactValue('', item))
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([childKey, child]) => [childKey, redactValue(childKey, child)]))
  return value
}

interface Props { items: EventItem[]; loading: boolean; error: string; hasMore: boolean; onMore: () => void; onRetry: () => void }
export function Logs({ items, loading, error, hasMore, onMore, onRetry }: Props) {
  const [filter, setFilter] = useState('all')
  const filtered = useMemo(() => items.filter((item) => filter === 'all' || item.level.toLowerCase() === filter), [items, filter])
  return <section className="panel logs" role="region" aria-labelledby="logs-title"><div className="panel__heading"><div><h2 id="logs-title">运行日志</h2><p>敏感字段在显示前再次脱敏</p></div><label className="filter">日志类别<select value={filter} onChange={(e) => setFilter(e.target.value)}><option value="all">全部</option><option value="info">信息</option><option value="warning">警告</option><option value="error">错误</option></select></label></div>
    <div className="log-list" aria-live="polite">{error ? <div className="empty"><p>日志加载失败。</p><button className="button" onClick={onRetry}>重试加载日志</button></div> : filtered.length ? filtered.map((item) => <article className="log-item" key={item.id}><time>{new Date(item.created_at).toLocaleString('zh-CN')}</time><strong>{item.event_type}</strong><span className={`level level--${item.level.toLowerCase()}`}>{item.level}</span><pre>{JSON.stringify(redactValue('', item.payload), null, 2)}</pre></article>) : <div className="empty">{items.length ? '当前筛选下没有日志。' : '暂无运行日志。开始监控后，关键事件会显示在这里。'}</div>}</div>
    <div className="logs-footer"><button className="button" disabled={loading || !hasMore} onClick={onMore}>{loading ? '正在加载' : hasMore ? '加载更多日志' : '没有更多日志。'}</button></div>
  </section>
}
