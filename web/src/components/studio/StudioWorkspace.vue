<script setup lang="ts">
import { computed, onScopeDispose, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../../api'
import { useSession } from '../../composables/useSession'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import { shortId, statusLabel } from '../../utils/display'
import PageHeader from '../shared/PageHeader.vue'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
import GenerationForm from './GenerationForm.vue'
import ClarificationForm from './ClarificationForm.vue'
import PlanPreview from './PlanPreview.vue'
const route = useRoute()
const router = useRouter()
const { state, role } = useSession()
const id = computed(() => String(route.params.id || ''))
const sources = useLoad(signal => api.datasources(state.projectId, signal), () => state.projectId)
const history = useLoad(signal => api.generations(state.projectId, signal), () => state.projectId)
const result = useLoad(signal => api.generation(id.value, signal), () => id.value)
const action = useAction()
let poll: ReturnType<typeof setTimeout> | undefined
watch(() => result.data.value, value => { clearTimeout(poll); if (value?.status === 'running') poll = setTimeout(() => void result.reload(), 5000) })
onScopeDispose(() => clearTimeout(poll))
function generate(body: { requirement: string; source_datasource_id: string; target_datasource_id: string }) {
  return action.run(() => api.generate({ ...body, project_id: state.projectId }), async item => { await router.push(`/studio/${item.run_id}`) })
}
function resume(answers: Record<string, string>) { return action.run(() => api.resume(id.value, answers), async () => { await Promise.all([result.reload(), history.reload()]) }) }
function select(event: Event) { const value = (event.target as HTMLSelectElement).value; void router.push(value ? `/studio/${value}` : '/studio') }
</script>
<template>
  <PageHeader :title="result.data.value?.status === 'interrupted' ? '还需要补充一些信息' : 'Pipeline Studio'" description="描述目标，检查生成结果，再进入版本与质量配置。"><RouterLink v-if="id" to="/studio" class="btn">新建需求</RouterLink></PageHeader><nav class="tabs"><RouterLink to="/studio" class="selected">方案生成</RouterLink><RouterLink to="/versions">版本与质量</RouterLink><RouterLink to="/governance">算子能力</RouterLink></nav>
  <div class="stack"><Feedback :error="action.error.value || result.error.value || sources.error.value || history.error.value" /><label class="history-select">生成历史<select :value="id" @change="select"><option value="">新建需求</option><option v-for="item in history.data.value ?? []" :key="item.run_id" :value="item.run_id">{{ item.plan?.name || shortId(item.run_id) }} / {{ statusLabel(item.status) }}</option></select></label><div class="two-col two-col-left"><GenerationForm :sources="sources.data.value ?? []" :busy="action.busy.value" :editable="role === 'maker'" @submit="generate" /><div class="stack"><EmptyState v-if="result.loading.value && !result.data.value || action.busy.value" title="正在生成方案" description="生成可能需要一些时间。请勿重复提交，已有任务可在生成历史中查询。" loading /><template v-else-if="result.data.value"><ClarificationForm v-if="result.data.value.status === 'interrupted'" :key="JSON.stringify(result.data.value.questions)" :questions="result.data.value.questions" :busy="action.busy.value" :editable="role === 'maker'" @submit="resume" /><Feedback v-else-if="result.data.value.status === 'unsupported'" :note="result.data.value.unsupported_reason || '当前受管算子无法完成此需求，请调整需求范围。'" /><Feedback v-else-if="result.data.value.status === 'failed'" :error="result.data.value.error || '生成失败，请查看需求和数据源配置。'" /><PlanPreview v-else :generation="result.data.value" /></template><EmptyState v-else title="从一句需求开始" description="选择已探查的来源和目标，描述要同步的表、字段与处理方式。" /></div></div><Feedback note="生成不等于执行。只有冻结版本、完成双维审批并由操作员提交后，数据作业才会开始。" /></div>
</template>
<style scoped>.history-select{max-width:470px}</style>
