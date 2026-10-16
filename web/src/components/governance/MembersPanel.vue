<script setup lang="ts">
import { reactive, shallowRef } from 'vue'
import type { Member, Role } from '../../types'
import { roleLabels } from '../../utils/display'
import { api } from '../../api'
import { useSession } from '../../composables/useSession'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import MemberRow from './MemberRow.vue'
import Feedback from '../shared/Feedback.vue'
import ConfirmDialog from '../shared/ConfirmDialog.vue'
const { state, role, refresh } = useSession()
const members = useLoad(signal => api.members(state.projectId, signal), () => state.projectId)
const action = useAction()
const form = reactive({ email: '', role: 'auditor' as Role })
const removing = shallowRef<Member | null>(null)
function add() { return action.run(() => api.addMember(state.projectId, form.email.trim(), form.role), async () => { form.email = ''; await members.reload(); await refresh() }, '成员已加入项目。') }
function update(member: Member, value: Role) { return action.run(() => api.updateMember(state.projectId, member.id, value), async () => { await members.reload(); await refresh() }, '角色已更新，服务端权限立即生效。') }
async function remove() { const member = removing.value; if (!member) return; await action.run(() => api.removeMember(state.projectId, member.id), async () => { removing.value = null; await members.reload(); await refresh() }, '成员已移出项目。') }
</script>
<template><section class="panel stack"><h2>项目成员</h2><Feedback :error="action.error.value || members.error.value" :success="action.success.value" /><form v-if="role === 'maker'" class="field-grid member-add" @submit.prevent="add"><label>已注册账号邮箱<input v-model="form.email" type="email" required autocomplete="off" placeholder="成员需先注册账号" /></label><label>项目角色<select v-model="form.role"><option v-for="(label, value) in roleLabels" :key="value" :value="value">{{ label }}</option></select></label><button class="btn btn-primary" :disabled="action.busy.value">邀请成员</button></form><div class="table-wrap"><table><thead><tr><th>成员</th><th>角色</th><th v-if="role === 'maker'">操作</th></tr></thead><tbody><MemberRow v-for="member in members.data.value ?? []" :key="member.id" :member="member" :manage="role === 'maker'" :busy="action.busy.value" @update="update(member, $event)" @remove="removing = member" /></tbody></table></div><p class="small muted">工程师管理项目成员；其他角色只读。角色设置不能绕过申请人自批与双维职责分离校验。</p></section><ConfirmDialog :open="!!removing" title="将该成员移出项目？" confirm-label="确认移出" :busy="action.busy.value" @close="removing = null" @confirm="remove"><p>{{ removing?.email }} 将失去当前项目的访问权限，账号本身不会被删除。</p></ConfirmDialog></template>
<style scoped>.member-add{align-items:end;grid-template-columns:minmax(180px,1fr) minmax(170px,250px) auto}@media(max-width:750px){.member-add{grid-template-columns:1fr}}</style>
