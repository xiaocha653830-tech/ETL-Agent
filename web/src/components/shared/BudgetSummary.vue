<script setup lang="ts">
import { computed } from 'vue'
import { bytes, count } from '../../utils/display'
const props = defineProps<{ budget: Record<string, number> }>()
const rows = computed(() => [
  ['行数上限', props.budget.max_rows === undefined ? '未声明' : count(props.budget.max_rows)],
  ['写入上限', props.budget.max_bytes === undefined ? '未声明' : bytes(props.budget.max_bytes)],
  ['运行时长', props.budget.max_duration_seconds === undefined ? '未声明' : `${props.budget.max_duration_seconds} 秒`],
  ['拒绝率', props.budget.max_reject_ratio === undefined ? '未声明' : `≤ ${props.budget.max_reject_ratio * 100}%`],
  ['放大比', props.budget.max_amplification_ratio === undefined ? '未声明' : `≤ ${props.budget.max_amplification_ratio}`],
])
</script>
<template><dl class="kv-list"><div v-for="[label, value] in rows" :key="label" class="kv"><dt>{{ label }}</dt><dd>{{ value }}</dd></div></dl></template>
