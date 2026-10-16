<script setup lang="ts">
import { shallowRef, computed } from 'vue'
import type { VersionSummary } from '../../types'
import { api } from '../../api'
import { useAction } from '../../composables/useAction'
import { pretty } from '../../utils/display'
import Feedback from '../shared/Feedback.vue'
const props = defineProps<{ versions: VersionSummary[]; current: string }>()
const against = shallowRef('')
const action = useAction()
const result = shallowRef<Awaited<ReturnType<typeof api.diff>> | null>(null)
const choices = computed(() => props.versions.filter(item => item.id !== props.current))
function compare() { return action.run(() => api.diff(props.current, against.value), value => { result.value = value }) }
</script>
<template><details class="panel stack"><summary>比较版本</summary><div class="stack comparison-body"><form class="inline" @submit.prevent="compare"><label class="comparison-select">对照版本<select v-model="against" required><option value="" disabled>选择版本</option><option v-for="item in choices" :key="item.id" :value="item.id">v{{ item.version_no }}</option></select></label><button class="btn" :disabled="action.busy.value || !against">比较</button></form><Feedback :error="action.error.value" /><template v-if="result"><div class="actions"><span class="badge">方案{{ result.plan_changed ? '已变化' : '未变化' }}</span><span class="badge">配置{{ result.config_changed ? '已变化' : '未变化' }}</span><span class="badge">规则{{ result.quality_rules_changed ? '已变化' : '未变化' }}</span></div><pre class="code-block">{{ result.config_diff.join('\n') || '配置无差异' }}</pre><details><summary>字段与算子变化</summary><pre class="code-block">{{ pretty({ mappings_added: result.mappings_added, mappings_removed: result.mappings_removed, mappings_changed: result.mappings_changed, steps_added: result.steps_added, steps_removed: result.steps_removed }) }}</pre></details></template></div></details></template>
<style scoped>.comparison-body{margin-top:20px}.comparison-select{flex:1;max-width:350px}.comparison-body>form{align-items:end}summary{cursor:pointer;font-weight:500}</style>
