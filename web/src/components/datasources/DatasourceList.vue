<script setup lang="ts">
import type { Datasource } from '../../types'
import StatusBadge from '../shared/StatusBadge.vue'
defineProps<{ sources: Datasource[]; selectedId?: string; canManage: boolean; busy: boolean }>()
const emit = defineEmits<{ select: [id: string]; test: [id: string]; profile: [id: string] }>()
</script>
<template><div class="table-wrap panel source-table"><table><thead><tr><th>名称</th><th>类型</th><th>连接状态</th><th>凭据</th><th>操作</th></tr></thead><tbody><tr v-for="source in sources" :key="source.id" :class="{ 'selected-row': selectedId === source.id }"><td><button class="text-button" @click="emit('select', source.id)">{{ source.name }}</button><span class="sub">{{ source.config.database || '—' }}</span></td><td>{{ source.type }}</td><td><StatusBadge :status="source.status" /></td><td>{{ source.has_credential ? '已托管' : '未配置' }}</td><td><div class="actions"><button v-if="canManage" class="btn btn-small" :disabled="busy" @click="emit('test', source.id)">测试连接</button><button v-if="canManage" class="btn btn-small" :disabled="busy" @click="emit('profile', source.id)">只读探查</button><button v-else class="text-button" @click="emit('select', source.id)">查看探查</button></div></td></tr></tbody></table></div></template>
<style scoped>.source-table{padding:0}</style>
