<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { Plus, Search } from 'lucide-vue-next'
import { api } from '../../api'
import { useSession } from '../../composables/useSession'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import PageHeader from '../shared/PageHeader.vue'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
import DatasourceForm from './DatasourceForm.vue'
import DatasourceList from './DatasourceList.vue'
import ProfileViewer from './ProfileViewer.vue'
const { state, role } = useSession()
const sources = useLoad(signal => api.datasources(state.projectId, signal), () => state.projectId)
const selection = shallowRef('')
const search = shallowRef('')
const creating = shallowRef(false)
const selected = computed(() => sources.data.value?.find(source => source.id === selection.value) ?? sources.data.value?.[0])
const visible = computed(() => sources.data.value?.filter(source => `${source.name} ${source.type}`.toLowerCase().includes(search.value.toLowerCase())) ?? [])
const profiles = useLoad(signal => api.profiles(selected.value!.id, signal), () => selected.value?.id)
const action = useAction()
async function create(body: Parameters<typeof api.createDatasource>[0] extends infer T ? Omit<T, 'project_id'> : never) {
  await action.run(() => api.createDatasource({ ...body, project_id: state.projectId }), async source => { creating.value = false; selection.value = source.id; await sources.reload() }, '连接已登记，请继续测试与探查。')
}
async function test(id: string) { await action.run(() => api.testDatasource(id), async result => { action.success.value = `${result.message}（${result.latency_ms} ms）`; await sources.reload() }) }
async function profile(id: string) { selection.value = id; await action.run(() => api.profile(id), async () => { await Promise.all([sources.reload(), profiles.reload()]) }, '探查完成，已保存脱敏字段快照。') }
</script>
<template>
  <PageHeader title="数据源" description="连接、测试并探查数据，为方案生成提供可信依据。"><button v-if="role === 'maker'" class="btn btn-primary" @click="creating = !creating"><Plus :size="16" />登记数据源</button></PageHeader>
  <nav class="tabs"><RouterLink to="/dashboard">总览</RouterLink><RouterLink to="/datasources" class="selected">数据源</RouterLink><RouterLink to="/governance?tab=members">项目成员</RouterLink></nav>
  <div class="stack"><Feedback :error="action.error.value || sources.error.value || profiles.error.value" :success="action.success.value" /><DatasourceForm v-if="creating" :busy="action.busy.value" @submit="create" @close="creating = false" /><div class="inline spread"><span class="muted">全部连接 {{ sources.data.value?.length ?? 0 }}</span><label class="search-input"><Search :size="17" /><input v-model="search" aria-label="搜索数据源" placeholder="搜索名称或类型" /></label></div><EmptyState v-if="sources.loading.value && !sources.data.value" title="加载数据源" loading /><DatasourceList v-else-if="visible.length" :sources="visible" :selected-id="selected?.id" :can-manage="role === 'maker'" :busy="action.busy.value" @select="selection = $event" @test="test" @profile="profile" /><EmptyState v-else title="没有找到数据源" description="尝试调整搜索条件，或由工程师登记第一个连接。" /><ProfileViewer v-if="selected" :key="selected.id" :source="selected" :profiles="profiles.data.value ?? []" :loading="profiles.loading.value" /></div>
</template>
<style scoped>.search-input{flex-direction:row;align-items:center;gap:8px;max-width:280px}.search-input input{padding:8px 11px}</style>
