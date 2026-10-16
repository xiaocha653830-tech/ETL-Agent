<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../../api'
import { useSession } from '../../composables/useSession'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import { shortId, statusLabel } from '../../utils/display'
import PageHeader from '../shared/PageHeader.vue'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import PreparationEvidence from './PreparationEvidence.vue'
import ApprovalActions from './ApprovalActions.vue'
const route = useRoute()
const router = useRouter()
const { state, role } = useSession()
const list = useLoad(signal => api.preparations(state.projectId, signal), () => state.projectId)
const id = computed(() => String(route.params.id || list.data.value?.[0]?.id || ''))
const detail = useLoad(signal => api.preparation(id.value, signal), () => id.value)
const action = useAction()
const names = computed(() => Object.fromEntries(state.members.map(item => [item.user_id, item.display_name || item.email])))
function choose(event: Event) { void router.push(`/approvals/${(event.target as HTMLSelectElement).value}`) }
function decide(approvalId: string, decision: 'approved' | 'rejected', comment: string) { return action.run(() => api.decide(approvalId, decision, comment), async () => { await Promise.all([detail.reload(), list.reload()]) }, '审批决定已记录。') }
function commit() { const prep = detail.data.value; if (prep) return action.run(() => api.commit(prep), async result => { await router.push(`/runs/${result.run_id}`) }) }
</script>
<template><PageHeader title="准备单与审批" description="核对影响范围与执行预算，分别完成数据和安全审批。"><RouterLink v-if="detail.data.value" :to="`/versions/${detail.data.value.version_id}`" class="btn">查看冻结版本</RouterLink></PageHeader><nav class="tabs"><RouterLink to="/approvals" class="selected">审批队列</RouterLink><RouterLink to="/runs">运行中心</RouterLink></nav><div class="stack"><Feedback :error="action.error.value || list.error.value || detail.error.value" :success="action.success.value" /><label v-if="list.data.value?.length" class="prep-select">准备单<select :value="id" :disabled="action.busy.value" @change="choose"><option v-for="item in list.data.value" :key="item.id" :value="item.id">{{ shortId(item.id) }} / {{ statusLabel(item.status) }} / {{ item.risk_level }}</option></select></label><EmptyState v-if="(list.loading.value || detail.loading.value) && !detail.data.value" title="加载准备单" loading /><template v-else-if="detail.data.value"><div class="panel inline spread summary-bar"><span class="mono">{{ shortId(detail.data.value.id) }}</span><span class="badge badge-solid">风险 {{ detail.data.value.risk_level }}</span><span class="small muted">申请人：{{ names[detail.data.value.created_by] || shortId(detail.data.value.created_by) }}</span><StatusBadge :status="detail.data.value.status" /></div><div class="two-col"><PreparationEvidence :preparation="detail.data.value" /><ApprovalActions :key="detail.data.value.id" :preparation="detail.data.value" :role="role" :user-id="state.user?.id || ''" :names="names" :busy="action.busy.value" @decide="decide" @commit="commit" /></div></template><EmptyState v-else title="暂无准备单" description="工程师冻结版本后，生成准备单开启双维审批。"><RouterLink to="/versions" class="btn">查看版本</RouterLink></EmptyState></div></template>
<style scoped>.prep-select{max-width:560px}.summary-bar{flex-wrap:wrap;padding:18px 24px}</style>
