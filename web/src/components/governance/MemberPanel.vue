<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { Plus } from 'lucide-vue-next'
import { api } from '../../api'
import { useSession } from '../../composables/useSession'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import { dateTime, roleLabels, shortId } from '../../utils/display'
import type { Role } from '../../types'
import EmptyState from '../shared/EmptyState.vue'
import Feedback from '../shared/Feedback.vue'
import ConfirmDialog from '../shared/ConfirmDialog.vue'
const { state, role } = useSession()
const list = useLoad(signal => api.members(state.projectId, signal), () => state.projectId)
const action = useAction()
const adding = shallowRef(false)
const email = shallowRef('')
const newRole = shallowRef<Role>('maker')
const removing = shallowRef<{ id: string; label: string } | null>(null)
/** 入口收敛：最终以服务端角色校验为准，这里只是不让没有权限的人点。 */
const editable = computed(() => role.value === 'maker')
const roles = Object.entries(roleLabels) as [Role, string][]
function add() { return action.run(() => api.addMember(state.projectId, email.value.trim(), newRole.value), async () => { adding.value = false; email.value = ''; await list.reload() }, '成员已添加。') }
function update(member: string, event: Event) { const value = (event.target as HTMLSelectElement).value as Role; return action.run(() => api.updateMember(state.projectId, member, value), async () => { await list.reload() }, '角色已更新。') }
function confirmRemove() { const target = removing.value; if (!target) return; return action.run(() => api.removeMember(state.projectId, target.id), async () => { removing.value = null; await list.reload() }, '成员已移除。') }
</script>
<template><div class="stack"><Feedback :error="action.error.value || list.error.value" :success="action.success.value" /><section class="panel stack"><div class="panel-title"><h2>项目成员</h2><button v-if="editable" class="btn btn-small" :disabled="action.busy.value" @click="adding = !adding"><Plus :size="14" />添加成员</button></div><form v-if="adding && editable" class="field-grid" @submit.prevent="add"><label>邮箱<input v-model="email" type="email" required placeholder="member@example.com" :disabled="action.busy.value" /></label><label>角色<select v-model="newRole" :disabled="action.busy.value"><option v-for="[value, label] in roles" :key="value" :value="value">{{ label }}</option></select></label><div class="actions"><button class="btn btn-primary" :disabled="action.busy.value">添加</button><button type="button" class="btn" @click="adding = false">取消</button></div></form><EmptyState v-if="list.loading.value && !list.data.value" title="加载成员" loading /><div v-else-if="list.data.value?.length" class="table-wrap"><table><thead><tr><th>成员</th><th>角色</th><th>加入时间</th><th></th></tr></thead><tbody><tr v-for="item in list.data.value" :key="item.id"><td>{{ item.display_name || item.email }}<span class="sub mono">{{ shortId(item.user_id) }}</span></td><td><select v-if="editable" :value="item.role" :disabled="action.busy.value" @change="update(item.id, $event)"><option v-for="[value, label] in roles" :key="value" :value="value">{{ label }}</option></select><span v-else>{{ roleLabels[item.role] }}</span></td><td>{{ dateTime(item.created_at) }}</td><td><button v-if="editable" class="text-button" :disabled="action.busy.value" @click="removing = { id: item.id, label: item.display_name || item.email }">移除</button></td></tr></tbody></table></div><p class="small muted">五角色互相独立：工程师提交方案、数据与安全审批人各审一维、操作员放行执行、审计员全局只读。同一人不可同时占用两个审批维度。</p></section><ConfirmDialog :open="!!removing" title="移除这个成员？" confirm-label="确认移除" :busy="action.busy.value" @close="removing = null" @confirm="confirmRemove"><p>{{ removing?.label }} 将失去该项目下的全部权限。已完成的历史记录不受影响。</p></ConfirmDialog></div></template>
