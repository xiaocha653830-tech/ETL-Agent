<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { Download, RefreshCw } from 'lucide-vue-next'
import { api } from '../../api'
import { download } from '../../api/client'
import { useSession } from '../../composables/useSession'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'
import { dateTime, shortId } from '../../utils/display'
import PageHeader from '../shared/PageHeader.vue'
import Feedback from '../shared/Feedback.vue'
import EmptyState from '../shared/EmptyState.vue'
/**
 * 链完整性的四种处境，措辞必须分开。
 *
 * `empty` 不能写成「校验通过」——「没有证据」与「证据可信」是两回事；
 * `indeterminate` 也不能写成「不通过」——那是「验不了」（密钥轮换出去），
 * 与「验了不通过」是完全不同的处置路径。后端为此特意分了四种状态，
 * 界面把它们合并显示就等于把这份区分抹掉。
 */
const CHAIN_STATUS: Record<string, { label: string; tone: 'ok' | 'bad' | 'warn' | 'muted'; note: string }> = {
  verified: { label: '校验通过', tone: 'ok', note: '整条链逐条复算一致，签名检查点全部验证通过。' },
  empty: { label: '尚无记录', tone: 'muted', note: '链上还没有任何证据。没有证据不等于证据可信。' },
  breached: { label: '校验不通过', tone: 'bad', note: '链已被改动。授权类操作（提交执行、清理残留）在此之前会被服务端拒绝。' },
  indeterminate: { label: '无法完整验证', tone: 'warn', note: '哈希链逐条完好，但存在验不了的签名检查点（通常是密钥轮换）。这不等于被篡改，也不阻断授权。' },
  unverified: { label: '尚未校验', tone: 'muted', note: '还没有跑过完整校验。' },
}
const { state } = useSession()
const records = useLoad(signal => api.auditRecords(state.projectId, { limit: 100 }, signal), () => state.projectId)
const verification = useLoad(signal => api.auditVerification(state.projectId, false, signal), () => state.projectId)
const action = useAction()
const expanded = shallowRef<number | null>(null)
const chain = computed(() => CHAIN_STATUS[verification.data.value?.status ?? 'unverified'] ?? CHAIN_STATUS.unverified)
const failures = computed(() => verification.data.value?.failures ?? [])
/** 服务端标注的「这条结论是旧的」——链在校验之后又前进过。 */
const mustRefresh = computed(() => verification.data.value?.stale ?? false)
function rerun() { return action.run(() => api.auditVerification(state.projectId, true), async () => { await Promise.all([verification.reload(), records.reload()]) }, '已完成一次完整校验。') }
function exportReport(format: 'json' | 'ndjson') { return action.run(() => download(api.auditReportPath(state.projectId, format), `audit.${format}`), undefined, '报告已开始下载；它是自包含的，可离线复算。') }
</script>
<template><PageHeader title="审计台" description="按时间回溯每一次受管动作，并核验证据链是否被改动过。"><button class="btn" :disabled="action.busy.value" @click="rerun"><RefreshCw :size="16" />重新校验</button></PageHeader><div class="stack"><Feedback :error="action.error.value || records.error.value || verification.error.value" :success="action.success.value" />
  <section class="panel stack"><div class="panel-title"><h2>链完整性</h2><span class="badge" :class="{ 'badge-solid': chain.tone === 'ok' }">{{ chain.label }}</span></div>
    <p class="small muted">{{ chain.note }}</p>
    <div v-if="mustRefresh" class="notice"><div><strong>这条结论是旧的</strong><p>链头在本次校验之后又前进了。点「重新校验」拿一份当前结论。</p></div></div>
    <template v-if="verification.data.value"><div class="metric-row"><div class="metric"><span class="metric-label">已校验记录</span><strong class="metric-value">{{ verification.data.value.checked }}</strong><span class="metric-caption">seq {{ verification.data.value.from_seq ?? '—' }} – {{ verification.data.value.to_seq ?? '—' }}</span></div><div class="metric"><span class="metric-label">签名检查点</span><strong class="metric-value">{{ verification.data.value.anchors_verified }}/{{ verification.data.value.anchors_checked }}</strong><span class="metric-caption">已验证 / 总数</span></div><div class="metric"><span class="metric-label">未锚定尾部</span><strong class="metric-value">{{ verification.data.value.unanchored_tail_seconds === null ? '—' : `${Math.round(verification.data.value.unanchored_tail_seconds / 60)} 分` }}</strong><span class="metric-caption">距上次锚定</span></div><div class="metric"><span class="metric-label">触发方式</span><strong class="metric-value">{{ verification.data.value.trigger }}</strong><span class="metric-caption">{{ dateTime(verification.data.value.checked_at) }}</span></div></div>
      <div v-if="chain.tone === 'bad' && failures.length" class="table-wrap"><table><thead><tr><th>位置</th><th>失败码</th><th>说明</th></tr></thead><tbody><tr v-for="item in failures" :key="item.seq + item.code"><td class="mono">seq {{ item.seq }}</td><td class="mono">{{ item.code }}</td><td>{{ item.detail }}</td></tr></tbody></table></div>
      <p class="small muted">公钥（{{ verification.data.value.public_key.algorithm }} / {{ verification.data.value.public_key.kid }}）随报告一并导出，第三方可离线复算整条链。</p></template>
    <EmptyState v-else-if="verification.loading.value" title="加载校验结论" loading />
  </section>
  <section class="panel stack"><div class="panel-title"><h2>事件链</h2><div class="actions"><button class="btn btn-small" :disabled="action.busy.value" @click="exportReport('json')"><Download :size="14" />导出 JSON</button><button class="btn btn-small" :disabled="action.busy.value" @click="exportReport('ndjson')"><Download :size="14" />导出 NDJSON</button></div></div>
    <EmptyState v-if="records.loading.value && !records.data.value" title="加载事件链" loading />
    <EmptyState v-else-if="!records.data.value?.items.length" title="链上还没有记录" description="完成一次 Prepare 后，证据会开始出现在这里。" />
    <div v-else class="table-wrap"><table><thead><tr><th>顺序</th><th>动作</th><th>主体</th><th>时间</th><th>摘要</th></tr></thead><tbody><tr v-for="item in records.data.value.items" :key="item.seq" class="record-row" @click="expanded = expanded === item.seq ? null : item.seq"><td class="mono">#{{ item.seq }}<span v-if="item.anchored" class="sub">检查点</span></td><td class="mono">{{ item.event_type }}</td><td class="mono">{{ item.actor }}</td><td>{{ dateTime(item.occurred_at) }}</td><td class="mono">{{ shortId(item.entry_hash) }}</td></tr></tbody></table></div>
    <details v-if="expanded !== null" class="panel"><summary>查看 #{{ expanded }} 的载荷</summary><pre class="code-block">{{ JSON.stringify(records.data.value?.items.find(item => item.seq === expanded)?.payload ?? null, null, 2) }}</pre></details>
    <p class="small muted">本页全局只读：审计面没有任何写操作。</p>
  </section>
</div></template>
<style scoped>.record-row{cursor:pointer}.record-row:hover{background:#f7f7f7}</style>
