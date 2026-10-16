import { onScopeDispose, shallowRef } from 'vue'
import { errorMessage } from '../api/client'

export function useAction() {
  const busy = shallowRef(false)
  const error = shallowRef('')
  const success = shallowRef('')
  let active = true
  onScopeDispose(() => { active = false })
  async function run<T>(task: () => Promise<T>, done?: (result: T) => void | Promise<void>, message = '') {
    if (busy.value) return
    busy.value = true
    error.value = ''
    success.value = ''
    try {
      const result = await task()
      if (active) { await done?.(result); success.value = message }
      return result
    } catch (failure) { if (active) error.value = errorMessage(failure) }
    finally { if (active) busy.value = false }
  }
  return { busy, error, success, run }
}
