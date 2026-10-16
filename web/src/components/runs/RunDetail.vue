<script setup lang="ts">
import type { Run, Role, Preparation, RollbackResult } from '../../types'
import { dateTime, shortId } from '../../utils/display'
import StatusBadge from '../shared/StatusBadge.vue'
import BudgetSummary from '../shared/BudgetSummary.vue'
import Feedback from '../shared/Feedback.vue'
import RunMetrics from './RunMetrics.vue'
import RunActions from './RunActions.vue'
import QualityReportPanel from './QualityReportPanel.vue'
defineProps<{ run: Run; role?: Role; busy: boolean; preparation: Preparation | null; rollbackResult: RollbackResult | null }>()
const emit = defineEmits<{ cancel: []; rollback: [] }>()
</script>
<template><section class="panel stack"><div class="panel-title"><h2>运行 {{ shortId(run.id) }}</h2><StatusBadge :status="run.status" :stopping="run.cancel_requested" /></div><p class="small muted">环境：{{ run.environment }} / 版本指纹 {{ shortId(run.artifact) }}</p><Feedback v-if="run.error" :error="run.error" /><RunMetrics :run="run" /><hr class="divider" /><dl class="kv-list"><div class="kv"><dt>正式表发布</dt><dd>{{ run.published_at ? `已发布 / ${dateTime(run.published_at)}` : run.published_at === undefined ? '当前接口未提供' : '尚未发布' }}</dd></div><div class="kv"><dt>残留清理</dt><dd>{{ run.rolled_back_at ? `已清理 / ${dateTime(run.rolled_back_at)}` : run.rolled_back_at === undefined ? '当前接口未提供' : '尚未清理' }}</dd></div><div class="kv"><dt>开始时间</dt><dd>{{ dateTime(run.started_at) }}</dd></div><div class="kv"><dt>最近心跳</dt><dd>{{ dateTime(run.heartbeat_at) }}</dd></div><div class="kv"><dt>结束时间</dt><dd>{{ dateTime(run.finished_at) }}</dd></div></dl><details v-if="preparation" class="budget-details"><summary>批准的执行预算</summary><BudgetSummary :budget="preparation.budget" /></details><Feedback note="质量报告详情暂不可查看；当前可核对拒绝行数与正式表发布时间。未提供的预算观测值不显示推算曲线。" /><div v-if="run.status === 'failed'" class="stack-sm"><h3>下一步处理</h3><p class="small muted">检查冻结版本中的质量规则、连接配置与源字段。调整方案后重新生成、冻结和审批，再提交新的运行。</p><p class="small muted">自动根因分析尚未开放；以上为通用处理指引。</p></div><RunActions :run="run" :role="role" :busy="busy" :preparation="preparation" :result="rollbackResult" @cancel="emit('cancel')" @rollback="emit('rollback')" /></section></template>
<style scoped>.budget-details{background:var(--canvas);padding:18px;border-radius:6px}.budget-details summary{cursor:pointer;font-weight:500;margin-bottom:14px}</style>
