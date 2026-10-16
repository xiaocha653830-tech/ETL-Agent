<script setup lang="ts">
import { shallowRef } from 'vue'
import { api } from '../../api'
import { useLoad } from '../../composables/useLoad'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
const operators = useLoad(signal => api.operators(signal))
const selected = shallowRef('')
const detail = useLoad(signal => api.operator(selected.value, signal), () => selected.value)
</script>
<template><section class="panel stack"><div class="panel-title"><h2>受管算子</h2><span class="badge">{{ operators.data.value?.length ?? 0 }} 个在册</span></div><Feedback :error="operators.error.value || detail.error.value" /><p class="small muted">查询属性与约束，确认方案是否超出受管能力。在册不代表当前引擎已经安装对应驱动。</p><EmptyState v-if="operators.loading.value" title="加载算子清单" loading /><div v-else class="table-wrap"><table><thead><tr><th>算子</th><th>角色</th><th>说明</th><th>操作</th></tr></thead><tbody><tr v-for="operator in operators.data.value ?? []" :key="operator.operator_id"><td class="mono">{{ operator.operator_id }}</td><td>{{ operator.type }}</td><td>{{ operator.description }}</td><td><button class="text-button" @click="selected = operator.operator_id">查看属性</button></td></tr></tbody></table></div><section v-if="detail.data.value" class="stack"><hr class="divider" /><h3>{{ detail.data.value.operator_id }}</h3><p class="small muted">SeaTunnel {{ detail.data.value.seatunnel_version }} / {{ detail.data.value.connector_artifact }}</p><div class="table-wrap"><table><thead><tr><th>属性</th><th>类型</th><th>必填</th><th>说明</th></tr></thead><tbody><tr v-for="attribute in detail.data.value.attributes" :key="attribute.name"><td>{{ attribute.name }}</td><td>{{ attribute.type }}</td><td>{{ attribute.required ? '是' : '否' }}</td><td>{{ attribute.description }}</td></tr></tbody></table></div><p v-for="constraint in detail.data.value.dialect_constraints" :key="constraint" class="small muted">{{ constraint }}</p></section></section></template>
