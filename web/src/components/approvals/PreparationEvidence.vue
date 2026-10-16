<script setup lang="ts">
import type { Preparation } from '../../types'
import { pretty, dateTime } from '../../utils/display'
import BudgetSummary from '../shared/BudgetSummary.vue'
defineProps<{ preparation: Preparation }>()
</script>
<template><section class="panel stack"><h2>这次执行会影响什么</h2><dl class="kv-list"><div class="kv"><dt>来源表</dt><dd>{{ preparation.impact_scope.source_table || '查看完整影响范围' }}</dd></div><div class="kv"><dt>正式目标</dt><dd>{{ preparation.impact_scope.target_table || '—' }}</dd></div><div class="kv"><dt>本次写入</dt><dd class="mono">{{ preparation.impact_scope.shadow_table || '—' }}</dd></div><div class="kv"><dt>有效期至</dt><dd>{{ dateTime(preparation.expires_at) }}</dd></div></dl><hr class="divider" /><h3>批准的执行预算</h3><BudgetSummary :budget="preparation.budget" /><hr class="divider" /><h3>风险依据</h3><ul v-if="preparation.risk_reasons.length" class="reason-list"><li v-for="reason in preparation.risk_reasons" :key="reason">{{ reason }}</li></ul><p v-else class="muted small">服务端未返回额外风险说明。</p><details><summary>数据分级、影响范围与回滚方案</summary><pre class="code-block">{{ pretty({ data_grading: preparation.data_grading, impact_scope: preparation.impact_scope, rollback_plan: preparation.rollback_plan }) }}</pre></details><div><p class="small muted">冻结指纹</p><p class="mono">{{ preparation.fingerprint }}</p></div><p class="small muted">准备单只描述影响；创建准备单时尚未启动数据作业。</p></section></template>
<style scoped>.reason-list{padding-left:20px;margin:0;color:var(--muted);font-size:13px}summary{cursor:pointer;font-weight:500;margin-bottom:12px}</style>
