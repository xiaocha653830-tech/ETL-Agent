import { describe, expect, it } from 'vitest'
import { ApiError, query } from '../../src/api/client'

describe('query', () => {
  it('剔除 undefined 与空串，保留 0 与 false 这类合法值', () => {
    // 空值会被后端当成「这个筛选项没给」，所以必须在序列化时就去掉；
    // 而 0 是合法取值（比如"只看第 0 页"），不能一起被剔掉。
    expect(query({ a: 1, b: undefined, c: '', d: 'x', zero: 0 })).toBe('a=1&d=x&zero=0')
  })

  it('空对象给出空串，调用方拼 URL 时不必特判', () => {
    expect(query({})).toBe('')
  })
})

describe('ApiError', () => {
  it('带上状态码与错误码，供调用方分支', () => {
    const error = new ApiError('令牌无效', 401, 'AUTH_TOKEN_INVALID')
    expect(error.message).toBe('令牌无效')
    expect(error.status).toBe(401)
    expect(error.code).toBe('AUTH_TOKEN_INVALID')
    expect(error).toBeInstanceOf(Error)
  })
})
