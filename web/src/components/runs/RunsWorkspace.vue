<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../../api'
import { useSession } from '../../composables/useSession'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import { dateTime, shortId, statusLabel } from '../../utils/display'
import PageHeader from '../shared/PageHeader.vue'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
import RunSummary from './RunSummary.vue'
import QualityReportPanel from './QualityReportPanel.vue'
import RunActions from './RunActions.vue'
const route = useRoute()
const router = useRouter()
const { state, role } = useSession()
const list = useLoad(signal => api.runs(state.projectId, signal), () => state.projectId)
// 与既有页面同形：URL 里的 :id 优先，否则回落到列表首项——URL 即选中态
const id = computed(() => String(route.params.id || list.data.value?.[0]?.id || ''))
const detail = useLoad(signal => api.run(id.value, signal), () => id.value)
const action = useAction()
function choose(event: Event) { void router.push(`/runs/${(event.target as HTMLSelectElement).value}`) }
function cancel() {
  const run = detail.data.value
  if (run) return action.run(() => api.cancel(run.id), async () => { await Promise.all([detail.reload(), list.reload()]) }, '已请求取消，执行层会在安全点停下。')
}
function rollback() {
  const run = detail.data.value
  if (run) return action.run(() => api.rollback(run.id), async () => { await Promise.all([detail.reload(), list.reload()]) }, '已清理影子表残留。')
}
</script>
<template><PageHeader title="运行中心" description="跟踪每一次受管执行，必要时请求取消或清理未发布的残留。"><RouterLink v-if="detail.data.value" :to="`/versions/${detail.data.value.version_id}`" class="btn">查看冻结版本</RouterLink></PageHeader><nav class="tabs"><RouterLink to="/approvals">审批队列</RouterLink><RouterLink to="/runs" class="selected">运行中心</RouterLink></nav><div class="stack"><Feedback :error="action.error.value || list.error.value || detail.error.value" :success="action.success.value" /><label v-if="list.data.value?.length" class="run-select">运行记录<select :value="id" :disabled="action.busy.value" @change="choose"><option v-for="item in list.data.value" :key="item.id" :value="item.id">{{ shortId(item.id) }} / {{ statusLabel(item.status) }} / {{ dateTime(item.created_at) }}</option></select></label><EmptyState v-if="(list.loading.value || detail.loading.value) && !detail.data.value" title="加载运行记录" loading /><template v-else-if="detail.data.value"><div class="two-col"><div class="stack"><RunSummary :run="detail.data.value" /><QualityReportPanel :run="detail.data.value" /></div><RunActions :key="detail.data.value.id" :run="detail.data.value" :role="role" :busy="action.busy.value" @cancel="cancel" @rollback="rollback" /></div></template><EmptyState v-else title="还没有执行记录" description="在审批队列完成双维审批并由操作员提交后，运行会出现在这里。"><RouterLink to="/approvals" class="btn">前往审批队列</RouterLink></EmptyState></div></template>
<style scoped>.run-select{max-width:520px}</style>
