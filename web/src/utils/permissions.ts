import type { Preparation, Role, Run } from '../types'
import { terminal } from './display'

export function approvalRestriction(prep: Preparation, userId: string, role?: Role, now = Date.now()): string | null {
  if (!['checker_data', 'checker_security'].includes(role || '')) return '当前角色没有审批权限。'
  if (prep.created_by === userId) return '不能审批自己提交的准备单。'
  if (prep.status === 'committed') return '准备单已提交执行，不能再次审批。'
  if (now >= Date.parse(prep.expires_at)) return '准备单已过期。'
  const dimension = role === 'checker_data' ? 'data' : 'security'
  if (prep.approvals.some(item => item.dimension !== dimension && item.approver_id === userId)) return '同一个人不能占用两个审批维度。'
  if (!prep.approvals.some(item => item.dimension === dimension)) return '当前维度没有审批槽位。'
  return null
}
export function commitRestriction(prep: Preparation, role?: Role, now = Date.now()): string | null {
  if (role !== 'operator') return '由操作员单独提交执行。'
  if (now >= Date.parse(prep.expires_at)) return '准备单已过期，请重新申请。'
  if (prep.status === 'committed') return '该准备单已提交，请前往运行中心。'
  if (prep.status !== 'approved' || prep.missing_dimensions.length) return '尚未完成双维审批。'
  if (!/^[a-f0-9]{64}$/i.test(prep.fingerprint)) return '版本指纹不可用，请刷新准备单。'
  return null
}
export function cancelRestriction(run: Run, role?: Role): string | null {
  if (role !== 'operator') return '只有操作员可以请求取消运行。'
  if (terminal(run.status)) return '运行已结束，无需取消。'
  if (run.cancel_requested) return '已请求取消，等待执行层在安全点停下。'
  return null
}
export function rollbackRestriction(run: Run, role?: Role): string | null {
  if (role !== 'operator') return '只有操作员可以清理运行残留。'
  if (!terminal(run.status)) return '请先取消，并等待运行结束。'
  if (run.published_at) return '已发布的运行不支持通过此操作撤销发布。'
  return null
}
