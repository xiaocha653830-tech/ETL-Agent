<script setup lang="ts">
import type { Run } from '../../types'
import { bytes, count } from '../../utils/display'
defineProps<{ run: Run }>()
</script>
<template><section class="metric-row three"><div class="metric"><span class="metric-label">读取行数</span><strong class="metric-value">{{ count(run.read_rows) }}</strong><span class="metric-caption">累计读取</span></div><div class="metric"><span class="metric-label">写入字节</span><strong class="metric-value">{{ bytes(run.written_bytes) }}</strong><span class="metric-caption">当前回传值</span></div><div class="metric"><span class="metric-label">拒绝行数</span><strong class="metric-value">{{ count(run.rejected_rows) }}</strong><span class="metric-caption">以运行记录为准</span></div></section></template>
