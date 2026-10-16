import { computed } from 'vue'
import { api } from '../api'
import { useSession } from './useSession'
import { useLoad } from './useLoad'
export function useDashboard() {
  const { state } = useSession()
  const load = useLoad(async signal => {
    const [sources, versions, preparations, runs] = await Promise.all([api.datasources(state.projectId, signal), api.versions(state.projectId, signal), api.preparations(state.projectId, signal), api.runs(state.projectId, signal)])
    return { sources, versions, preparations, runs }
  }, () => state.projectId)
  const frozen = computed(() => load.data.value?.versions.filter(item => item.status === 'frozen').length ?? 0)
  const pending = computed(() => load.data.value?.preparations.filter(item => item.status === 'pending') ?? [])
  const latestRuns = computed(() => load.data.value?.runs.slice(0, 5) ?? [])
  const rate = computed(() => {
    const runs = load.data.value?.runs ?? []
    return runs.length ? `${Math.round(runs.filter(run => run.status === 'succeeded').length / runs.length * 100)}%` : '—'
  })
  const versionLabel = (id: string) => { const item = load.data.value?.versions.find(version => version.id === id); return item ? `版本 v${item.version_no}` : '版本详情' }
  return { ...load, frozen, pending, latestRuns, rate, versionLabel }
}
