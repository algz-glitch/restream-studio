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
    <div className="monitor-scroll">
      <table className="monitor-table" aria-label="来源与输出实时指标">
        <thead><tr><th scope="col">通道</th><th scope="col">状态</th><th scope="col">输入</th><th scope="col">FPS</th><th scope="col">码率</th><th scope="col">速度</th><th scope="col">运行时长</th><th scope="col">重连</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.kind}><td><strong>{row.name}</strong>{row.last_error && <small className="row-error">{row.last_error}</small>}</td><td><StatusBadge state={row.status} /></td><td>{row.input}</td><td className="mono">{value(row.fps)}</td><td className="mono">{value(row.bitrate_kbps, ' kb/s')}</td><td className="mono">{value(row.speed, '×')}</td><td className="mono">{duration(row.uptime_seconds)}</td><td className="mono">{value(row.reconnect_count)}</td></tr>)}</tbody>
      </table>
    </div>
  </section>
}
