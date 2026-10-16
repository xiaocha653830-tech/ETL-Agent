<script setup lang="ts">
import { FolderOpen, LoaderCircle } from 'lucide-vue-next'
defineProps<{ title: string; description?: string; loading?: boolean }>()
</script>
<template><div class="empty-state" :aria-busy="loading"><component :is="loading ? LoaderCircle : FolderOpen" :size="30" :class="{ spinning: loading }" /><h3>{{ loading ? '正在加载…' : title }}</h3><p v-if="description">{{ description }}</p><div v-if="$slots.default" class="actions"><slot /></div></div></template>
