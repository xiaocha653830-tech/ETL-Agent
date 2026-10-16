import { computed, reactive, readonly } from 'vue'
import { api } from '../api'
import { ApiError, errorMessage, TOKEN_KEY } from '../api/client'
import type { Member, Project, User } from '../types'

const state = reactive({ token: sessionStorage.getItem(TOKEN_KEY) || '', user: null as User | null, projects: [] as Project[], members: [] as Member[], projectId: sessionStorage.getItem('etl.project') || '', loading: false, error: '' })
let pendingRestore: Promise<void> | undefined
let sessionRevision = 0
let projectRevision = 0
const role = computed(() => state.members.find(member => member.user_id === state.user?.id)?.role)
const project = computed(() => state.projects.find(item => item.id === state.projectId))

function logout() {
  sessionRevision++
  projectRevision++
  Object.assign(state, { token: '', user: null, projects: [], members: [], projectId: '', error: '', loading: false })
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem('etl.project')
  pendingRestore = undefined
}

async function selectProject(projectId: string) {
  if (!state.projects.some(item => item.id === projectId)) return
  const revision = ++projectRevision
  state.members = []
  state.projectId = projectId
  sessionStorage.setItem('etl.project', projectId)
  const members = await api.members(projectId)
  if (revision === projectRevision) state.members = members
}

async function refresh() {
  const revision = sessionRevision
  state.loading = true
  state.error = ''
  try {
    const [user, projects] = await Promise.all([api.me(), api.projects()])
    if (revision !== sessionRevision) return
    state.user = user
    state.projects = projects
    const chosen = projects.find(item => item.id === state.projectId) ?? projects[0]
    if (chosen) await selectProject(chosen.id)
    else { state.projectId = ''; state.members = [] }
  } catch (error) {
    if (revision !== sessionRevision) return
    if (error instanceof ApiError && error.status === 401) logout()
    else state.error = errorMessage(error)
  } finally { if (revision === sessionRevision) state.loading = false }
}

async function login(email: string, password: string) {
  const token = await api.login(email, password)
  logout()
  state.token = token.access_token
  sessionStorage.setItem(TOKEN_KEY, token.access_token)
  await refresh()
  if (!state.token) throw new Error('登录会话无效，请重新登录。')
}

function restore() {
  if (!pendingRestore) pendingRestore = state.token ? refresh() : Promise.resolve()
  return pendingRestore
}

window.addEventListener('etl:unauthorized', logout)
export function useSession() {
  return { state: readonly(state), role, project, login, logout, restore, refresh, selectProject }
}
