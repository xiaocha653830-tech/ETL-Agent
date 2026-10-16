<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { LockKeyhole } from 'lucide-vue-next'
import PageHeader from '../components/shared/PageHeader.vue'
import OperatorsPanel from '../components/governance/OperatorsPanel.vue'
import MembersPanel from '../components/governance/MembersPanel.vue'
const route = useRoute()
const members = computed(() => route.query.tab === 'members')
</script>
<template><PageHeader title="安全治理" description="查看项目权限、受管能力与治理功能的开放范围。" /><nav class="tabs"><RouterLink to="/governance" :class="{ selected: !members }">算子能力</RouterLink><RouterLink to="/governance?tab=members" :class="{ selected: members }">成员与角色</RouterLink><button disabled>审计账本 · 待开放</button><button disabled>Benchmark · 待开放</button></nav><div class="two-col"><MembersPanel v-if="members" /><OperatorsPanel v-else /><aside class="panel stack"><LockKeyhole :size="25" /><h2>完整证据账本与评测</h2><p class="muted small">哈希链校验、审计报告导出与 Benchmark 尚未开放。当前可查询准备单、冻结版本与运行事实。</p><button class="btn" disabled>查看审计报告</button><button class="btn" disabled>运行 Benchmark</button><hr class="divider" /><h3>角色各司其职</h3><p class="small muted">工程师生成并冻结方案；两位审批人独立批准；操作员单独提交执行。审计员只读查看。</p><p class="small muted">普通运行日志不能代替防篡改证据。</p></aside></div></template>
