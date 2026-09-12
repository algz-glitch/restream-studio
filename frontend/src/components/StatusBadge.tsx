const labels: Record<string, string> = {
  OFFLINE: '离线', LIVE: '直播中', RECONNECTING: '重连中', AUTH_FAILED: '认证失败',
  ERROR: '异常', STANDBY: '待命', STOPPED: '离线', CONNECTING: '连接中', RUNNING: '直播中',
}

export function stateLabel(state: string): string { return labels[state.toUpperCase()] ?? state }

export function StatusBadge({ state }: { state: string }) {
  const key = state.toUpperCase()
  return <span className={`status status--${key.toLowerCase()}`}><span className="status__shape" aria-hidden="true" />{stateLabel(key)}</span>
}
