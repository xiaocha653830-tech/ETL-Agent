import { createRouter, createWebHistory } from 'vue-router'
import { useSession } from '../composables/useSession'

/**
 * 路由表。
 *
 * **外壳与页面分开**：`/login` 是独立全屏页，其余全部挂在 `AppLayout` 之下——
 * 侧边栏与顶栏只应出现一次，挂在父路由上就不会随着页面切换被反复重建。
 *
 * **详情页不是子路由**：`/runs/:id` 这类形状在既有三个页面里是惯例
 * （`route.params.id` 优先，回落到列表首项），同一组件既渲染列表又渲染详情，
 * 页内不嵌套 `<RouterView>`。这样 URL 本身就是选中态——可深链、可分享、
 * 刷新后仍在同一处。
 *
 * 页面组件一律**懒加载**：首屏只需要登录页或外壳。
 */

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
  },
  {
    path: '/',
    component: () => import('../components/layout/AppLayout.vue'),
    children: [
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', name: 'dashboard', component: () => import('../views/DashboardView.vue') },
      { path: 'datasources', name: 'datasources', component: () => import('../views/DatasourcesView.vue') },
      { path: 'studio', name: 'studio', component: () => import('../views/StudioView.vue') },
      { path: 'studio/:id', name: 'studio-run', component: () => import('../views/StudioView.vue') },
      { path: 'versions', name: 'versions', component: () => import('../views/VersionsView.vue') },
      { path: 'versions/:id', name: 'version-detail', component: () => import('../views/VersionsView.vue') },
      { path: 'approvals', name: 'approvals', component: () => import('../views/ApprovalsView.vue') },
      { path: 'approvals/:id', name: 'preparation-detail', component: () => import('../views/ApprovalsView.vue') },
      { path: 'runs', name: 'runs', component: () => import('../views/RunsView.vue') },
      { path: 'runs/:id', name: 'run-detail', component: () => import('../views/RunsView.vue') },
      { path: 'governance', name: 'governance', component: () => import('../views/GovernanceView.vue') },
      { path: 'audit', name: 'audit', component: () => import('../views/AuditView.vue') },
    ],
  },
  // 未知路径回落而不是停在空白页——旧书签或手输错路径时给人一个着落点
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/**
 * 未登录守卫。
 *
 * 只管「有没有令牌」，**不管令牌是否有效**——有效性由服务端判定，前端的
 * 任何预判都只是提示。真正失效时 `api/client.ts` 会发出 `etl:unauthorized`，
 * 由 `useSession` 统一登出并把人送回这里。
 */
router.beforeEach(async (to) => {
  const session = useSession()
  await session.restore()
  const token = session.state.token
  if (!token && to.path !== '/login') return { path: '/login' }
  if (token && to.path === '/login') return { path: '/dashboard' }
  return true
})
