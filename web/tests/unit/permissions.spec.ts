import { describe, expect, it } from 'vitest'
import { approvalRestriction, cancelRestriction, commitRestriction, rollbackRestriction } from '../../src/utils/permissions'
import type { Preparation, Run } from '../../src/types'

/**
 * 这些判定在前端只是「入口收敛」——真正的门在服务端。但它们必须与服务端口径一致：
 * 前端说「可以点」而服务端拒绝，会让人以为系统坏了；反过来让人对着点不动的按钮
 * 发问，同样是摩擦。这些用例钉的就是这份一致性。
 */
const NOW = Date.parse('2026-10-16T10:00:00Z')
const FUTURE = '2026-10-17T10:00:00Z'
const PAST = '2026-10-15T10:00:00Z'

function run(overrides: Partial<Run> = {}): Run {
  return {
    id: 'run-1', project_id: 'p', version_id: 'v', preparation_id: 'prep',
    status: 'failed', environment: 'dev', artifact: 'a'.repeat(64),
    read_rows: 0, written_bytes: 0, rejected_rows: 0, cancel_requested: false,
    worker_lease_owner: null, lease_expires_at: null, error: null,
    started_at: null, finished_at: null, heartbeat_at: null,
    published_at: null, rolled_back_at: null, quality_report: null,
    created_at: FUTURE, ...overrides,
  }
}

function preparation(overrides: Partial<Preparation> = {}): Preparation {
  return {
    id: 'prep', version_id: 'v', project_id: 'p', status: 'pending', risk_level: 'P1',
    fingerprint: 'f'.repeat(64), expires_at: FUTURE, created_at: FUTURE,
    created_by: 'maker-1', impact_scope: {}, data_grading: {}, budget: {},
    rollback_plan: {}, risk_reasons: [],
    approvals: [
      { id: 'a1', dimension: 'data', status: 'pending', approver_id: null, decision: null, comment: null, decided_at: null, expires_at: FUTURE },
      { id: 'a2', dimension: 'security', status: 'pending', approver_id: null, decision: null, comment: null, decided_at: null, expires_at: FUTURE },
    ],
    missing_dimensions: ['data', 'security'], committed_at: null, ...overrides,
  }
}

describe('approvalRestriction', () => {
  it('非审批角色不能审', () => {
    expect(approvalRestriction(preparation(), 'u1', 'maker', NOW)).toContain('没有审批权限')
  })

  it('不能审自己提交的（FR-24 职责分离）', () => {
    expect(approvalRestriction(preparation(), 'maker-1', 'checker_data', NOW)).toContain('自己提交')
  })

  it('已提交执行的不能再审', () => {
    expect(approvalRestriction(preparation({ status: 'committed' }), 'u1', 'checker_data', NOW)).toContain('已提交')
  })

  it('过期就不能再审', () => {
    expect(approvalRestriction(preparation({ expires_at: PAST }), 'u1', 'checker_data', NOW)).toContain('已过期')
  })

  it('同一人不能占两个维度', () => {
    const prep = preparation({ approvals: [
      { id: 'a1', dimension: 'data', status: 'approved', approver_id: 'u1', decision: 'approved', comment: null, decided_at: FUTURE, expires_at: FUTURE },
      { id: 'a2', dimension: 'security', status: 'pending', approver_id: null, decision: null, comment: null, decided_at: null, expires_at: FUTURE },
    ] })
    expect(approvalRestriction(prep, 'u1', 'checker_security', NOW)).toContain('两个审批维度')
  })

  it('条件都满足时放行', () => {
    expect(approvalRestriction(preparation(), 'u1', 'checker_data', NOW)).toBeNull()
  })
})

describe('commitRestriction', () => {
  it('只有操作员能提交', () => {
    expect(commitRestriction(preparation(), 'maker', NOW)).toContain('操作员')
  })

  it('双维未齐不放行', () => {
    expect(commitRestriction(preparation({ status: 'pending' }), 'operator', NOW)).toContain('双维审批')
  })

  it('指纹不是 64 位 hex 就拦下', () => {
    const prep = preparation({ status: 'approved', missing_dimensions: [], fingerprint: 'not-a-hash' })
    expect(commitRestriction(prep, 'operator', NOW)).toContain('指纹')
  })

  it('齐备且指纹有效时放行', () => {
    const prep = preparation({ status: 'approved', missing_dimensions: [] })
    expect(commitRestriction(prep, 'operator', NOW)).toBeNull()
  })
})

describe('cancelRestriction', () => {
  it('只有操作员能取消', () => {
    expect(cancelRestriction(run({ status: 'running' }), 'maker')).toContain('操作员')
  })

  it('已结束的运行不用取消', () => {
    expect(cancelRestriction(run({ status: 'succeeded' }), 'operator')).toContain('已结束')
  })

  it('已请求过就不再重复请求', () => {
    expect(cancelRestriction(run({ status: 'running', cancel_requested: true }), 'operator')).toContain('已请求')
  })

  it('运行中可以取消', () => {
    expect(cancelRestriction(run({ status: 'running' }), 'operator')).toBeNull()
  })
})

describe('rollbackRestriction', () => {
  it('只有操作员能清理残留', () => {
    expect(rollbackRestriction(run(), 'maker')).toContain('操作员')
  })

  it('运行没结束不能清理', () => {
    expect(rollbackRestriction(run({ status: 'running' }), 'operator')).toContain('等待运行结束')
  })

  it('已发布的运行不能通过它撤销发布', () => {
    expect(rollbackRestriction(run({ published_at: FUTURE }), 'operator')).toContain('已发布')
  })

  it('未发布的失败运行可以清理', () => {
    expect(rollbackRestriction(run({ status: 'failed' }), 'operator')).toBeNull()
  })
})
