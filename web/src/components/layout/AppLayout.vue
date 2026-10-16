<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LayoutDashboard, Workflow, Activity, ShieldCheck, FileCheck2, LogOut, Menu, X, Folder, Plus, RefreshCw } from 'lucide-vue-next'
import { useSession } from '../../composables/useSession'
import { useAction } from '../../composables/useAction'
import { roleLabels } from '../../utils/display'
import { api } from '../../api'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
const { state, role, project, logout, refresh, selectProject } = useSession()
const route = useRoute()
const router = useRouter()
const mobileOpen = shallowRef(false)
const name = shallowRef('')
const creating = shallowRef(false)
const action = useAction()
const navigation = [
  { label: '工作台', to: '/dashboard', icon: LayoutDashboard, paths: ['/dashboard', '/datasources'] },
  { label: 'Pipeline Studio', to: '/studio', icon: Workflow, paths: ['/studio', '/versions'] },
  { label: '运行与审批', to: '/approvals', icon: Activity, paths: ['/approvals', '/runs'] },
  { label: '安全治理', to: '/governance', icon: ShieldCheck, paths: ['/governance'] },
  { label: '审计台', to: '/audit', icon: FileCheck2, paths: ['/audit'] },
]
const activeSection = computed(() => navigation.find(item => item.paths.some(path => route.path.startsWith(path))))
async function changeProject(event: Event) {
  await action.run(() => selectProject((event.target as HTMLSelectElement).value), () => router.replace('/dashboard').then(() => undefined))
}
async function createProject() {
  await action.run(() => api.createProject(name.value.trim()), async (item) => { await refresh(); await selectProject(item.id); creating.value = false; name.value = '' })
}
function leave() { logout(); void router.replace('/login') }
</script>
<template>
  <div class="app-layout">
    <button v-if="mobileOpen" class="nav-overlay" aria-label="关闭导航" @click="mobileOpen = false" />
    <aside class="sidebar" :class="{ 'sidebar-open': mobileOpen }">
      <div class="brand-row"><RouterLink to="/dashboard" class="brand">ETL-Agent</RouterLink><button class="mobile-only icon-button" aria-label="关闭导航" @click="mobileOpen = false"><X :size="20" /></button></div>
      <p class="sidebar-caption">数据集成控制台</p>
      <label class="project-switch"><Folder :size="16" /><select aria-label="切换项目" :value="state.projectId" :disabled="state.loading || action.busy.value" @change="changeProject"><option v-if="!state.projects.length" value="">尚无项目</option><option v-for="item in state.projects" :key="item.id" :value="item.id">{{ item.name }}</option></select></label>
      <nav aria-label="主导航"><RouterLink v-for="item in navigation" :key="item.to" :to="item.to" :class="{ active: activeSection?.to === item.to }" @click="mobileOpen = false"><component :is="item.icon" :size="19" />{{ item.label }}</RouterLink></nav>
      <footer class="sidebar-footer"><span>正式表发布需要双维审批</span><strong>{{ state.user?.display_name || state.user?.email || '当前用户' }}</strong><span>{{ role ? roleLabels[role] : '正在确认项目权限' }}</span><button class="sidebar-link" @click="creating = !creating"><Plus :size="15" />创建项目</button><button class="sidebar-link" @click="leave"><LogOut :size="15" />退出登录</button></footer>
    </aside>
    <div class="workspace"><header class="topbar"><div class="inline"><button class="mobile-only icon-button" aria-label="打开导航" @click="mobileOpen = true"><Menu :size="22" /></button><span>{{ project?.name || '工作空间' }} <span class="muted">/ {{ activeSection?.label }}</span></span></div><div class="inline"><button class="icon-button" title="刷新账号与项目权限" aria-label="刷新账号与项目权限" :disabled="state.loading" @click="refresh"><RefreshCw :size="16" /></button><span class="role-label">{{ role ? roleLabels[role] : '未选择项目' }}</span></div></header>
      <main class="page-content" id="main-content">
        <Feedback :error="state.error || action.error.value" />
        <form v-if="creating || (!state.projects.length && !state.loading && !state.error)" class="panel project-create" @submit.prevent="createProject"><h1>创建你的第一个项目</h1><p class="muted">连接、版本和审批都归属于项目。创建者自动成为工程师。</p><label>项目名称<input v-model="name" required maxlength="128" placeholder="例如：电商数据项目" /></label><button class="btn btn-primary" :disabled="action.busy.value">创建项目</button></form>
        <EmptyState v-else-if="state.loading" title="加载工作空间" loading />
        <RouterView v-else-if="state.projectId" :key="state.projectId + route.fullPath" />
        <EmptyState v-else-if="state.error" title="工作空间暂不可用" description="恢复连接后刷新，已有数据不会被替换。"><button class="btn" @click="refresh">重试连接</button></EmptyState>
      </main>
    </div>
  </div>
</template>
