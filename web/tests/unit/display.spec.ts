import { describe, expect, it } from 'vitest'
import { bytes, count, shortId, statusLabel, terminal } from '../../src/utils/display'

describe('bytes', () => {
  it('按 1024 进制换算并带单位', () => {
    expect(bytes(0)).toBe('0 B')
    expect(bytes(1024)).toBe('1 KiB')
    expect(bytes(1024 * 1024)).toBe('1 MiB')
  })

  it('非法值给破折号而不是 NaN', () => {
    // 运行度量可能缺失，界面不该显示 "NaN B"
    expect(bytes(-1)).toBe('—')
    expect(bytes(Number.NaN)).toBe('—')
  })
})

describe('statusLabel', () => {
  it('认识的取中文，不认识的原样返回', () => {
    expect(statusLabel('succeeded')).toBe('已成功')
    expect(statusLabel('breached')).toBe('breached')
  })
})

describe('terminal', () => {
  it('只有三种终态算结束', () => {
    expect(terminal('succeeded')).toBe(true)
    expect(terminal('failed')).toBe(true)
    expect(terminal('cancelled')).toBe(true)
    // 这两个都没结束——取消按钮与"清理残留"的判定全靠它
    expect(terminal('running')).toBe(false)
    expect(terminal('pending')).toBe(false)
  })
})

describe('shortId', () => {
  it('空值给「未生成」而不是空白', () => {
    expect(shortId(null)).toBe('未生成')
    expect(shortId(undefined)).toBe('未生成')
  })

  it('保留首尾便于人工比对', () => {
    // 前 8 位 + 后 6 位：UUID 靠尾部区分，靠头部对不上人眼
    expect(shortId('0123456789abcdef')).toBe('01234567…abcdef')
  })
})

describe('count', () => {
  it('按中文千分位', () => {
    expect(count(1234567)).toBe('1,234,567')
  })
})
