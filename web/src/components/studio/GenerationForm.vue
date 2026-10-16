<script setup lang="ts">
import { computed, reactive } from 'vue'
import type { Datasource } from '../../types'
const props = defineProps<{ sources: Datasource[]; busy: boolean; editable: boolean }>()
const emit = defineEmits<{ submit: [body: { requirement: string; source_datasource_id: string; target_datasource_id: string }] }>()
const form = reactive({ source_datasource_id: '', target_datasource_id: '', requirement: '' })
const targets = computed(() => props.sources.filter(source => source.type === 'doris'))
</script>
<template><form class="panel stack" @submit.prevent="emit('submit', { ...form })"><h2>描述你的集成需求</h2><fieldset class="stack" :disabled="busy || !editable"><label>来源<select v-model="form.source_datasource_id" required><option value="" disabled>选择来源连接</option><option v-for="source in sources" :key="source.id" :value="source.id">{{ source.name }} / {{ source.type }}</option></select></label><label>目标<select v-model="form.target_datasource_id" required><option value="" disabled>选择 Doris 目标连接</option><option v-for="source in targets" :key="source.id" :value="source.id">{{ source.name }}</option></select></label><label>自然语言需求<textarea v-model="form.requirement" required maxlength="4000" rows="6" placeholder="将 orders 的订单编号、用户名、金额和日期全量同步到 Doris 的订单宽表。" /></label><button class="btn btn-primary" type="submit">{{ busy ? '正在生成…' : '生成方案' }}</button></fieldset><p class="small muted">{{ editable ? '质量规则由工程师单独声明。系统遇到歧义时会先请求补充信息。' : '当前角色只读，方案生成由工程师操作。' }}</p><RouterLink v-if="!targets.length" class="text-button" to="/datasources">先登记并探查 Doris 目标连接</RouterLink></form></template>
