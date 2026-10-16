import { onScopeDispose, shallowRef, watch } from 'vue'
import { errorMessage } from '../api/client'

/** 每次刷新取消旧请求；路由/项目切换后不把旧响应写到新页面。 */
export function useLoad<T>(loader: (signal: AbortSignal) => Promise<T>, key: () => unknown = () => true) {
  const data = shallowRef<T | null>(null)
  const loading = shallowRef(false)
  const error = shallowRef('')
  let controller: AbortController | undefined
  let revision = 0
  async function reload() {
    const current = ++revision
    controller?.abort()
    if (!key()) { data.value = null; loading.value = false; return }
    controller = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const result = await loader(controller.signal)
      if (current === revision) data.value = result
    } catch (failure) {
      if (current === revision && !(failure instanceof DOMException && failure.name === 'AbortError')) error.value = errorMessage(failure)
    } finally { if (current === revision) loading.value = false }
  }
  watch(key, () => { data.value = null; void reload() }, { immediate: true })
  onScopeDispose(() => { revision++; controller?.abort() })
  return { data, loading, error, reload }
}
