<script setup lang="ts">
import { watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSession } from './composables/useSession'
const { state, restore } = useSession()
const router = useRouter()
// 带令牌刷新页面（F5、深链、书签）时必须把用户与项目重新拉一次。
// 少了这一步，`state.projects` 会一直是空的，而外壳据此判成「还没有项目」——
// 于是每个整页导航都停在「创建你的第一个项目」，看着像账号没有项目。
void restore()
watch(() => state.token, (token) => { if (!token) void router.replace('/login') })
</script>
<template><a class="skip-link" href="#main-content">跳到主要内容</a><RouterView /></template>
