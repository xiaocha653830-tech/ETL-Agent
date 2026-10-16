<script setup lang="ts">
import { computed } from 'vue'
import type { Run } from '../../types'
import { count } from '../../utils/display'
const props = defineProps<{ run: Run }>()
const report = computed(() => props.run.quality_report)
const distribution = computed(() => Object.entries(report.value?.error_dist ?? {}))
const ratio = computed(() => report.value ? `${(report.value.rejected_ratio * 100).toFixed(2)}%` : '—')
</script>
<template><section class="panel stack"><div class="panel-title"><h2>质量结论</h2><span v-if="report" class="badge">来自质量报告</span></div>
  <!--
    「没有报告」与「报告显示零拒绝」是两回事：前者是这次运行压根没声明质量规则，
    后者是规则确实跑过并全部通过。合并成同一个样子，等于把「没检查」说成「检查通过」。
  -->
  <div v-if="!report" class="notice"><div><strong>本次运行没有质量契约</strong><p>该版本未声明质量规则，因此没有分流、也没有错误表——这不表示数据已经过校验。</p></div></div>
  <template v-else>
    <div class="metric-row"><div class="metric"><span class="metric-label">源端行数</span><strong class="metric-value">{{ count(report.source_rows) }}</strong></div><div class="metric"><span class="metric-label">有效行</span><strong class="metric-value">{{ count(report.valid_rows) }}</strong></div><div class="metric"><span class="metric-label">被拒行</span><strong class="metric-value">{{ count(report.rejected_rows) }}</strong></div><div class="metric"><span class="metric-label">拒绝率</span><strong class="metric-value">{{ ratio }}</strong></div></div>
    <div v-if="distribution.length"><h3>按错误码的拒绝分布</h3><div class="table-wrap"><table><thead><tr><th>错误码</th><th>行数</th></tr></thead><tbody><tr v-for="[code, value] in distribution" :key="code"><td class="mono">{{ code }}</td><td>{{ count(value) }}</td></tr></tbody></table></div></div>
    <p v-else class="small muted">没有拒绝行，错误分布为空。</p>
    <p v-if="report.error_table" class="small muted">错误明细表：<span class="mono">{{ report.error_table }}</span></p>
  </template>
</section></template>
