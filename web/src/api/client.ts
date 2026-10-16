export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly code: string, public readonly detail?: unknown) { super(message) }
}

interface RequestOptions { method?: string; body?: unknown; signal?: AbortSignal; timeout?: number }
export const TOKEN_KEY = 'etl.session'

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  options.signal?.addEventListener('abort', abort, { once: true })
  if (options.signal?.aborted) controller.abort()
  const timer = window.setTimeout(abort, options.timeout ?? 120_000)
  const token = sessionStorage.getItem(TOKEN_KEY)
  try {
    const response = await fetch(`/api/v1${path}`, {
      method: options.method ?? 'GET', signal: controller.signal,
      headers: { Accept: 'application/json', ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
    })
    const payload = response.status === 204 ? null : await response.json().catch(() => null)
    if (!response.ok) {
      if (response.status === 401 && token && !path.startsWith('/auth/')) window.dispatchEvent(new Event('etl:unauthorized'))
      const validation = Array.isArray(payload?.detail) ? payload.detail.map((item: { msg: string }) => item.msg).join('；') : ''
      throw new ApiError(payload?.message || validation || `请求失败（${response.status}）`, response.status, payload?.code || 'HTTP_ERROR', payload?.detail)
    }
    return payload as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (controller.signal.aborted) {
      if (options.signal?.aborted) throw new DOMException('请求已取消', 'AbortError')
      throw new ApiError('请求超时。操作可能已被服务端受理，请刷新状态后再决定下一步。', 0, 'TIMEOUT')
    }
    throw new ApiError('无法连接服务，请检查后端地址与服务状态。', 0, 'NETWORK_ERROR')
  } finally {
    window.clearTimeout(timer)
    options.signal?.removeEventListener('abort', abort)
  }
}

export function query(values: Record<string, string | number | undefined>) {
  return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== undefined && value !== '').map(([key, value]) => [key, String(value)])).toString()
}

/**
 * 触发一次**带鉴权**的文件下载。
 *
 * 不用 `window.open(url)`：报告接口要求项目成员身份，而新窗口发出的请求带不上
 * `Authorization` 头——那会打开一个 401 的 JSON 页面，而不是下载文件。这里改用
 * `fetch` 把响应读成 Blob，再借一个临时 `<a download>` 落盘。文件名优先取服务端
 * 给的 `Content-Disposition`，取不到才回落到调用方给的兜底名。
 */
export async function download(path: string, fallbackName: string): Promise<void> {
  const token = sessionStorage.getItem(TOKEN_KEY)
  const response = await fetch(`/api/v1${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) {
    throw new ApiError(`导出失败（${response.status}）`, response.status, 'HTTP_ERROR')
  }
  const disposition = response.headers.get('content-disposition') ?? ''
  const filename = /filename="?([^";]+)"?/.exec(disposition)?.[1] || fallbackName
  const objectUrl = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(objectUrl)
}

export const errorMessage = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试。'
