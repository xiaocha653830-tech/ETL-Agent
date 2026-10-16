<script setup lang="ts">
import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { RefreshCw } from 'lucide-vue-next'
import { api } from '../../api'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import { useSession } from '../../composables/useSession'
import { terminal, shortId, dateTime } from '../../utils/display'
import type { RollbackResult } from '../../types'
import PageHeader from '../shared/PageHeader.vue'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import RunDetail from './RunDetail.vue'
const route = useRoute()
const router = useRouter()
const { state, role } = useSession()
const status = shallowRef('')
const list = useLoad(signal => api.runs(state.projectId, signal, status.value), () => `${state.projectId}:${status.value}`)
const id = computed(() => String(route.params.id || list.data.value?.[0]?.id || ''))
const detail = useLoad(async signal => { const run = await api.run(id.value, signal); if (run.project_id !== state.projectId) throw new Error('该运行属于其他项目，请先切换项目。'); return run }, () => id.value)
const preparation = useLoad(signal => api.preparation(detail.data.value!.preparation_id, signal), () => detail.data.value?.preparation_id)
const action = useAction()
const rollbackResult = shallowRef<RollbackResult | null>(null)
let timer: ReturnType<typeof setTimeout> | undefined
watch(() => detail.data.value, run => { clearTimeout(timer); if (run && !terminal(run.status)) timer = setTimeout(() => { void refresh() }, 5000) })
onScopeDispose(() => clearTimeout(timer))
async function refresh() { await Promise.all([list.reload(), detail.reload()]) }
function cancel() { return action.run(() => api.cancel(id.value), async () => { await refresh() }, '取消请求已受理，请等待运行结束。') }
function rollback() { return action.run(() => api.rollback(id.value), async result => { rollbackResult.value = result; await refresh() }) }
</script>
<template><PageHeader title="运行中心" description="跟踪数据作业，区分执行结果与正式表发布状态。"><button class="btn" :disabled="detail.loading.value || list.loading.value" @click="refresh"><RefreshCw :size="16" />刷新状态</button></PageHeader><nav class="tabs"><RouterLink to="/approvals">审批队列</RouterLink><RouterLink to="/runs" class="selected">运行中心</RouterLink></nav><div class="stack"><Feedback :error="action.error.value || detail.error.value || list.error.value || preparation.error.value" :success="action.success.value" /><div class="runs-grid"><aside class="panel stack"><h2>最近运行</h2><label>执行状态<select v-model="status"><option value="">全部状态</option><option value="pending">等待执行</option><option value="running">运行中</option><option value="succeeded">已成功</option><option value="failed">已失败</option><option value="cancelled">已取消</option></select></label><EmptyState v-if="list.loading.value && !list.data.value" title="加载运行列表" loading /><button v-for="run in list.data.value ?? []" :key="run.id" class="run-item" :class="{ 'run-item-selected': id === run.id }" @click="router.push(`/runs/${run.id}`)"><strong class="mono">{{ shortId(run.id) }}</strong><StatusBadge :status="run.status" :stopping="run.cancel_requested" /><span class="small">{{ dateTime(run.created_at) }}</span></button><p v-if="!list.data.value?.length && !list.loading.value" class="small muted">暂无匹配的执行记录。</p><p class="small muted">仅展示最近 100 次运行</p></aside><div><EmptyState v-if="detail.loading.value && !detail.data.value" title="加载运行详情" loading /><RunDetail v-else-if="detail.data.value" :run="detail.data.value" :role="role" :busy="action.busy.value" :preparation="preparation.data.value" :rollback-result="rollbackResult" @cancel="cancel" @rollback="rollback" /><EmptyState v-else title="选择一次运行" description="提交准备单后，执行记录会出现在这里。" /></div></div></div></template>
<style scoped>.runs-grid{display:grid;grid-template-columns:260px minmax(0,1fr);gap:22px;align-items:start}.run-item{display:flex;flex-direction:column;align-items:flex-start;gap:10px;border:1px solid transparent;border-radius:6px;padding:15px;background:var(--canvas);text-align:left;color:#000}.run-item-selected{border-color:#000}.run-item:hover{border-color:#aaa}.run-item>span:last-child{color:var(--muted)}@media(max-width:1050px){.runs-grid{grid-template-columns:minmax(0,1fr)}.runs-grid>aside{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.runs-grid>aside h2,.runs-grid>aside>label{grid-column:1/-1}}@media(max-width:500px){.runs-grid>aside{grid-template-columns:1fr}}</style>
