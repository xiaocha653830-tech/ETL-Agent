export const roleLabels = { maker: '工程师', checker_data: '数据审批人', checker_security: '安全审批人', operator: '操作员', auditor: '审计员' } as const
const statuses: Record<string, string> = { draft: '草稿', frozen: '已冻结', rejected: '已拒绝', pending: '待处理', approved: '已批准', committed: '已提交', expired: '已过期', running: '运行中', succeeded: '已成功', failed: '已失败', cancelled: '已取消', interrupted: '等待补充', unsupported: '暂不支持', registered: '已登记', connected: '连接正常', profiled: '已探查', unreachable: '连接失败' }
export const statusLabel = (value: string) => statuses[value] || value
export const dateTime = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)) : '—'
export const shortId = (value?: string | null) => value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未生成'
export const count = (value: number) => new Intl.NumberFormat('zh-CN').format(value)
export function bytes(value: number) {
  if (!Number.isFinite(value) || value < 0) return '—'
  const unit = value ? Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4) : 0
  return `${(value / 1024 ** unit).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][unit]}`
}
export const terminal = (status: string) => ['succeeded', 'failed', 'cancelled'].includes(status)
export const pretty = (value: unknown) => JSON.stringify(value, null, 2)
