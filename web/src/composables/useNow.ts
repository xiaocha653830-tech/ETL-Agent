import { onScopeDispose, shallowRef } from 'vue'
export function useNow() {
  const now = shallowRef(Date.now())
  const timer = setInterval(() => { now.value = Date.now() }, 1000)
  onScopeDispose(() => clearInterval(timer))
  return now
}
