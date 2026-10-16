<script setup lang="ts">
import { computed } from 'vue'
import { Check, Clock3, CircleAlert, LockKeyhole, LoaderCircle } from 'lucide-vue-next'
import { statusLabel } from '../../utils/display'
const props = defineProps<{ status: string; stopping?: boolean }>()
const label = computed(() => props.stopping && ['pending', 'running'].includes(props.status) ? '正在停止' : statusLabel(props.status))
const icon = computed(() => props.status === 'running' ? LoaderCircle : ['succeeded', 'approved', 'connected', 'profiled'].includes(props.status) ? Check : ['failed', 'rejected', 'unreachable'].includes(props.status) ? CircleAlert : props.status === 'frozen' ? LockKeyhole : Clock3)
</script>
<template><span class="badge" :class="{ 'badge-solid': ['succeeded', 'frozen', 'approved'].includes(status) }"><component :is="icon" :size="13" :class="{ spinning: status === 'running' }" />{{ label }}</span></template>
