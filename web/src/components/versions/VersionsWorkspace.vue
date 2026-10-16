<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../../api'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import { useSession } from '../../composables/useSession'
import type { QualityRule } from '../../types'
import { dateTime, statusLabel } from '../../utils/display'
import PageHeader from '../shared/PageHeader.vue'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
import QualityRuleEditor from './QualityRuleEditor.vue'
import VersionGate from './VersionGate.vue'
import VersionComparison from './VersionComparison.vue'
const route = useRoute()
const router = useRouter()
const { state, role } = useSession()
const list = useLoad(signal => api.versions(state.projectId, signal), () => state.projectId)
const id = computed(() => String(route.params.id || list.data.value?.[0]?.id || ''))
const detail = useLoad(signal => api.version(id.value, signal), () => id.value)
const fields = computed(() => detail.data.value?.etl_plan.field_mappings.map(item => item.target_field) ?? [])
const action = useAction()
const dirty = shallowRef(false)
function choose(event: Event) { void router.push(`/versions/${(event.target as HTMLSelectElement).value}`) }
function save(rules: QualityRule[]) { return action.run(() => api.qualityRules(id.value, rules), async () => { await detail.reload() }, '质量规则已保存。') }
async function freeze() { await action.run(() => api.freeze(id.value), undefined, '版本已冻结，可以生成准备单。'); await Promise.all([detail.reload(), list.reload()]) }
function prepare() { return action.run(() => api.prepare(id.value), async prep => { await router.push(`/approvals/${prep.id}`) }) }
</script>
<template><PageHeader :title="detail.data.value ? `${detail.data.value.etl_plan.name} / v${detail.data.value.version_no}` : '版本与质量'" description="先声明质量规则，再执行确定性门禁并冻结版本。" /><nav class="tabs"><RouterLink to="/studio">方案生成</RouterLink><RouterLink to="/versions" class="selected">版本与质量</RouterLink><RouterLink to="/governance">算子能力</RouterLink></nav><div class="stack"><Feedback :error="action.error.value || detail.error.value || list.error.value" :success="action.success.value" /><label v-if="list.data.value?.length" class="version-select">方案版本<select :value="id" :disabled="dirty || action.busy.value" @change="choose"><option v-for="item in list.data.value" :key="item.id" :value="item.id">v{{ item.version_no }} / {{ statusLabel(item.status) }} / {{ dateTime(item.created_at) }}</option></select></label><EmptyState v-if="(list.loading.value || detail.loading.value) && !detail.data.value" title="加载版本" loading /><template v-else-if="detail.data.value"><div class="two-col"><QualityRuleEditor :key="detail.data.value.id" :rules="detail.data.value.quality_rules" :fields="fields" :editable="role === 'maker' && detail.data.value.status === 'draft'" :busy="action.busy.value" @save="save" @dirty="dirty = $event" /><VersionGate :version="detail.data.value" :maker="role === 'maker'" :busy="action.busy.value" :dirty="dirty" @freeze="freeze" @prepare="prepare" /></div><details class="panel"><summary>查看只读配置</summary><pre class="code-block config-body">{{ detail.data.value.config }}</pre></details><VersionComparison :key="id" :versions="list.data.value ?? []" :current="id" /></template><EmptyState v-else title="还没有方案版本" description="在 Studio 完成生成后，草稿版本会出现在这里。"><RouterLink to="/studio" class="btn btn-primary">前往 Studio</RouterLink></EmptyState></div></template>
<style scoped>.version-select{max-width:480px}summary{cursor:pointer;font-weight:500}.config-body{margin-top:20px}</style>
