<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Plus, Trash2 } from 'lucide-vue-next'
import type { QualityRule } from '../../types'
import Feedback from '../shared/Feedback.vue'
const props = defineProps<{ rules: QualityRule[]; fields: string[]; editable: boolean; busy: boolean }>()
const emit = defineEmits<{ save: [rules: QualityRule[]]; dirty: [value: boolean] }>()
const rows = ref<{ key: string; value: QualityRule }[]>([])
watch(() => props.rules, rules => { rows.value = rules.map(value => ({ key: crypto.randomUUID(), value: JSON.parse(JSON.stringify(value)) as QualityRule })) }, { immediate: true })
const values = computed(() => rows.value.map(row => row.value))
const dirty = computed(() => JSON.stringify(values.value) !== JSON.stringify(props.rules))
watch(dirty, value => emit('dirty', value), { immediate: true })
const validation = computed(() => {
  if (values.value.length > 100) return '最多声明 100 条规则。'
  if (values.value.some(rule => !props.fields.includes(rule.field))) return '规则字段必须来自目标字段映射。'
  if (values.value.some(rule => !rule.error_code.trim())) return '每条规则都需要错误码。'
  if (new Set(values.value.map(rule => rule.error_code)).size !== values.value.length) return '错误码必须唯一。'
  if (values.value.some(rule => rule.rule === 'regex' && !String(rule.params.pattern || '').trim())) return '正则规则需要填写匹配表达式。'
  if (values.value.some(rule => !['not_null', 'regex'].includes(rule.rule) || rule.action !== 'reject')) return '当前数据面仅支持非空、正则的拒绝规则；请修改未支持的规则。'
  return ''
})
function add() { rows.value.push({ key: crypto.randomUUID(), value: { field: props.fields[0] || '', rule: 'not_null', params: {}, error_code: '', action: 'reject' } }) }
function save() { if (!validation.value) emit('save', JSON.parse(JSON.stringify(values.value))) }
</script>
<template>
  <form class="panel stack" @submit.prevent="save"><div class="panel-title"><h2>质量规则</h2><span class="badge">{{ rows.length }} 条规则</span></div><p class="small muted">规则由工程师声明；未通过的记录进入错误表。只有创建人可以保存自己的草稿规则。</p><Feedback v-if="dirty" :error="validation" /><fieldset class="stack" :disabled="!editable || busy"><article v-for="(row, index) in rows" :key="row.key" class="rule-editor"><div class="inline spread"><strong>规则 {{ index + 1 }}</strong><button v-if="editable" class="icon-button" type="button" :aria-label="`移除规则 ${index + 1}`" @click="rows.splice(index, 1)"><Trash2 :size="16" /></button></div><div class="field-grid"><label>目标字段<select v-model="row.value.field" required><option v-for="field in fields" :key="field" :value="field">{{ field }}</option></select></label><label>规则<select v-model="row.value.rule"><option value="not_null">非空</option><option value="regex">正则表达式</option><option value="range" disabled>范围（未开放）</option><option value="enum" disabled>枚举（未开放）</option></select></label><label>错误码<input v-model="row.value.error_code" required maxlength="64" placeholder="例如 ORDER_ID_NULL" /></label><label>不通过时<select v-model="row.value.action"><option value="reject">拒绝并分流</option><option value="warn" disabled>仅警告（未开放）</option></select></label></div><label v-if="row.value.rule === 'regex'">匹配表达式<input :value="String(row.value.params.pattern || '')" required placeholder="请输入正则表达式" @input="row.value.params.pattern = ($event.target as HTMLInputElement).value" /></label></article><p v-if="!rows.length" class="muted small">尚未声明质量规则。你可以添加规则，或明确保留空规则集。</p><div v-if="editable" class="actions spread"><button type="button" class="btn" :disabled="!fields.length || rows.length >= 100" @click="add"><Plus :size="15" />添加规则</button><button class="btn btn-primary" :disabled="!!validation || !dirty">保存质量规则</button></div></fieldset><p class="small muted">{{ editable ? '保存完成后再执行门禁。范围、枚举和仅警告规则暂未开放。' : '此版本或当前角色只读。冻结版本不可修改；变更需求需生成新版本。' }}</p></form>
</template>
<style scoped>.rule-editor{background:var(--canvas);border:1px solid var(--line);border-radius:6px;padding:18px;display:flex;flex-direction:column;gap:14px}.rule-editor strong{font-size:13px}</style>
