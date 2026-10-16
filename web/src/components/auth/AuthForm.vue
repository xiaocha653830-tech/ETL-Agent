<script setup lang="ts">
import { reactive, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, ShieldCheck } from 'lucide-vue-next'
import { api } from '../../api'
import { useSession } from '../../composables/useSession'
import { useAction } from '../../composables/useAction'
import Feedback from '../shared/Feedback.vue'
const router = useRouter()
const { login } = useSession()
const action = useAction()
const registering = shallowRef(false)
const fields = reactive({ email: '', password: '', display_name: '' })
async function submit() {
  await action.run(async () => {
    if (registering.value) await api.register({ ...fields, email: fields.email.trim() })
    await login(fields.email.trim(), fields.password)
  }, async () => { fields.password = ''; await router.replace('/dashboard') })
}
</script>
<template>
  <form class="auth-form" @submit.prevent="submit">
    <span class="badge">{{ registering ? '开始使用' : '欢迎回来' }}</span>
    <div><h1>{{ registering ? '注册项目账号' : '登录工作空间' }}</h1><p class="muted">{{ registering ? '注册后创建项目，或由项目负责人邀请加入。' : '使用项目账号继续工作。' }}</p></div>
    <Feedback :error="action.error.value" />
    <label v-if="registering">显示名称<input v-model="fields.display_name" required maxlength="64" autocomplete="name" placeholder="如何称呼你" /></label>
    <label>邮箱<input v-model="fields.email" required type="email" maxlength="255" autocomplete="username" placeholder="请输入邮箱地址" /></label>
    <label>密码<input v-model="fields.password" required type="password" :minlength="registering ? 8 : undefined" maxlength="128" :autocomplete="registering ? 'new-password' : 'current-password'" placeholder="请输入密码" /></label>
    <button class="btn btn-primary" :disabled="action.busy.value" type="submit">{{ action.busy.value ? '正在处理…' : registering ? '注册并登录' : '登录' }}<ArrowRight :size="16" /></button>
    <p class="small muted">{{ registering ? '已有账号？' : '还没有账号？' }} <button type="button" class="text-button" :disabled="action.busy.value" @click="registering = !registering; action.error.value = ''">{{ registering ? '返回登录' : '注册账号' }}</button></p>
    <hr class="divider" /><div class="inline small muted"><ShieldCheck :size="18" /><span>权限由项目角色决定<br />工程师 · 数据审批 · 安全审批 · 操作员 · 审计员</span></div>
  </form>
</template>
<style scoped>
.auth-form{width:min(400px,100%);display:flex;flex-direction:column;gap:24px}.auth-form>.badge{align-self:flex-start}.auth-form h1{margin-bottom:8px}.auth-form>.btn{align-self:flex-start;min-width:130px}.auth-form label{gap:9px}
</style>
