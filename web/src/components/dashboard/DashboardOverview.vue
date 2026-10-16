<script setup lang="ts">
import { ArrowUpRight, Database, GitBranch, CheckCheck } from 'lucide-vue-next'
import { useDashboard } from '../../composables/useDashboard'
import { dateTime, shortId } from '../../utils/display'
import StatusBadge from '../shared/StatusBadge.vue'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
const { data, error, loading, reload, frozen, pending, latestRuns, rate, versionLabel } = useDashboard()
</script>
<template>
  <Feedback :error="error" /><EmptyState v-if="loading && !data" title="加载项目总览" loading />
  <div v-if="data" class="stack">
    <section class="panel metric-row"><div class="metric"><span class="metric-label">数据源</span><strong class="metric-value">{{ data.sources.length.toString().padStart(2, '0') }}</strong><span class="metric-caption">当前项目连接</span></div><div class="metric"><span class="metric-label">冻结版本</span><strong class="metric-value">{{ frozen }}</strong><span class="metric-caption">共 {{ data.versions.length }} 个版本</span></div><div class="metric"><span class="metric-label">待审批</span><strong class="metric-value">{{ pending.length }}</strong><span class="metric-caption">仍有审批维度待完成</span></div><div class="metric"><span class="metric-label">近期成功率</span><strong class="metric-value">{{ rate }}</strong><span class="metric-caption">最近 {{ data.runs.length }} 次运行，最多 100 次</span></div></section>
    <section class="panel stack"><div class="panel-title"><h2>从连接到交付</h2><RouterLink to="/studio" class="text-button">进入 Studio</RouterLink></div><div class="delivery-flow"><RouterLink to="/datasources"><Database :size="23" /><div><h3>连接与探查</h3><p>为方案提供可信字段依据</p></div><ArrowUpRight :size="17" /></RouterLink><RouterLink to="/versions"><GitBranch :size="23" /><div><h3>版本与质量</h3><p>声明规则，校验并冻结</p></div><ArrowUpRight :size="17" /></RouterLink><RouterLink to="/approvals"><CheckCheck :size="23" /><div><h3>审批与发布</h3><p>独立审批，操作员提交</p></div><ArrowUpRight :size="17" /></RouterLink></div></section>
    <div class="two-col two-col-left"><section class="panel stack"><h2>需要跟进</h2><EmptyState v-if="!pending.length" title="暂无待审批准备单" description="冻结版本后可以生成准备单。" /><article v-for="item in pending.slice(0, 3)" :key="item.id" class="task-item"><div class="inline spread"><h3>{{ versionLabel(item.version_id) }}</h3><StatusBadge :status="item.status" /></div><p class="small muted">有效期至 {{ dateTime(item.expires_at) }}</p><RouterLink :to="`/approvals/${item.id}`" class="btn btn-small">查看准备单</RouterLink></article></section><section class="panel"><div class="panel-title"><h2>最近运行</h2><button class="icon-button" aria-label="刷新项目数据" @click="reload"><ArrowUpRight :size="18" /></button></div><EmptyState v-if="!latestRuns.length" title="尚无执行记录" description="完成双维审批并提交后，运行会出现在这里。" /><div v-else class="table-wrap"><table><thead><tr><th>版本 / 运行</th><th>执行状态</th><th>正式表</th><th>开始时间</th></tr></thead><tbody><tr v-for="run in latestRuns" :key="run.id"><td><RouterLink :to="`/runs/${run.id}`">{{ versionLabel(run.version_id) }}<span class="sub mono">{{ shortId(run.id) }}</span></RouterLink></td><td><StatusBadge :status="run.status" :stopping="run.cancel_requested" /></td><td>{{ run.published_at ? '已发布' : '未发布' }}</td><td>{{ dateTime(run.started_at) }}</td></tr></tbody></table></div></section></div>
  </div><button v-else-if="error" class="btn" @click="reload">重试加载</button>
</template>
<style scoped>
.delivery-flow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:28px}.delivery-flow>a{display:flex;align-items:center;gap:16px;padding:14px 0}.delivery-flow>a>div{flex:1}.delivery-flow p{font-size:12px;color:var(--muted);margin-top:5px}.task-item{display:flex;flex-direction:column;align-items:flex-start;gap:12px;border-bottom:1px solid var(--line);padding-bottom:18px}.task-item:last-child{border-bottom:0;padding-bottom:0}.task-item>.inline{width:100%}@media(max-width:950px){.delivery-flow{grid-template-columns:1fr;gap:6px}}
</style>
