<script setup lang="ts">
import { computed } from 'vue'
import { api } from '../../api'
import type { Run } from '../../types'
import { useLoad } from '../../composables/useLoad'
import { bytes, count, dateTime, shortId } from '../../utils/display'
import BudgetSummary from '../shared/BudgetSummary.vue'
import StatusBadge from '../shared/StatusBadge.vue'
const props = defineProps<{ run: Run }>()
/**
 * 预算挂在**准备单**上而不是运行上——批准的就是那一份。这里补一次加载，
 * 好让「批准了多少」与「实际用了多少」同屏可比；分在两处看，就没有人会去比。
 */
const preparation = useLoad(signal => api.preparation(props.run.preparation_id, signal), () => props.run.preparation_id)
const publishLabel = computed(() => props.run.published_at ? '已发布到正式表' : '未发布')
</script>
<template><section class="panel stack"><div class="panel-title"><h2>执行摘要</h2><StatusBadge :status="run.status" :stopping="run.cancel_requested" /></div><dl class="kv-list"><div class="kv"><dt>运行</dt><dd class="mono">{{ shortId(run.id) }}</dd></div><div class="kv"><dt>制品指纹</dt><dd class="mono">{{ shortId(run.artifact) }}</dd></div><div class="kv"><dt>环境</dt><dd>{{ run.environment }}</dd></div><div class="kv"><dt>发布状态</dt><dd>{{ publishLabel }}</dd></div><div class="kv" v-if="run.rolled_back_at"><dt>残留清理</dt><dd>{{ dateTime(run.rolled_back_at) }}</dd></div><div class="kv"><dt>开始 / 结束</dt><dd>{{ dateTime(run.started_at) }} → {{ dateTime(run.finished_at) }}</dd></div></dl><hr class="divider" /><div class="metric-row three"><div class="metric"><span class="metric-label">读取行数</span><strong class="metric-value">{{ count(run.read_rows) }}</strong></div><div class="metric"><span class="metric-label">写入字节</span><strong class="metric-value">{{ bytes(run.written_bytes) }}</strong></div><div class="metric"><span class="metric-label">拒绝行数</span><strong class="metric-value">{{ count(run.rejected_rows) }}</strong></div></div><p v-if="run.error" class="small muted">失败原因：{{ run.error }}</p><hr class="divider" /><div><h3>批准的执行预算</h3><BudgetSummary v-if="preparation.data.value" :budget="preparation.data.value.budget" /><p v-else class="small muted">{{ preparation.loading.value ? '加载准备单…' : '准备单不可用，预算未知。' }}</p></div></section></template>
