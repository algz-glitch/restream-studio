import type { StatusResponse } from '../types'
import { StatusBadge } from './StatusBadge'

const outputName: Record<string, string> = { douyin: '抖音输出', wechat_channels: '微信视频号输出' }
const value = (input: number | undefined, suffix = '') => input === undefined ? '—' : `${input}${suffix}`
const duration = (seconds?: number) => seconds === undefined ? '—' : new Date(seconds * 1000).toISOString().slice(11, 19)

export function Monitor({ status }: { status: StatusResponse }) {
  const outputs = (['douyin', 'wechat_channels'] as const).map((kind) => {
    const current = status.outputs.find((item) => item.kind === kind)
    return current ? { ...current, name: outputName[kind] } : { kind, name: outputName[kind], enabled: false, status: 'STANDBY', input: 'NONE', last_error: null }
  })
  const rows = [{ kind: 'source', name: '抖音来源', status: status.source_state, enabled: true, input: 'SOURCE', last_error: status.source_failure, fps: status.source_fps, bitrate_kbps: status.source_bitrate_kbps, speed: status.source_speed, uptime_seconds: status.uptime_seconds, reconnect_count: undefined }, ...outputs]
  return <section className="panel monitor" role="region" aria-labelledby="monitor-title"><div className="panel__heading"><div><h2 id="monitor-title">传输监控</h2><p>每 2 秒刷新，仅显示后端确认状态</p></div></div>
    <div className="monitor-table" role="table" aria-label="来源与输出实时指标">
      <div className="monitor-row monitor-head" role="row"><span>通道</span><span>状态</span><span>输入</span><span>FPS</span><span>码率</span><span>速度</span><span>运行时长</span><span>重连</span></div>
      {rows.map((row) => <div className="monitor-row" role="row" key={row.kind}><strong>{row.name}</strong><StatusBadge state={row.status} /><span>{row.input}</span><span className="mono">{value(row.fps)}</span><span className="mono">{value(row.bitrate_kbps, ' kb/s')}</span><span className="mono">{value(row.speed, '×')}</span><span className="mono">{duration(row.uptime_seconds)}</span><span className="mono">{value(row.reconnect_count)}</span>{row.last_error && <small className="row-error">{row.last_error}</small>}</div>)}
    </div>
  </section>
}
