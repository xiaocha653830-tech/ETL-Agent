<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import PageHeader from '../components/shared/PageHeader.vue'
import OperatorPanel from '../components/governance/OperatorPanel.vue'
import MemberPanel from '../components/governance/MemberPanel.vue'
/**
 * 两个 tab 由 `?tab=` 决定，而不是各自一个路由。
 *
 * 既有入口本来就有两套写法——`/governance?tab=members`（数据源页与总览页链向项目成员）
 * 与裸 `/governance`（Studio 与版本页链向算子能力）。合成一个页面、默认落在算子能力，
 * 两边的入口就都落到对的地方，不必去改那四处链接。
 */
const route = useRoute()
const tab = computed(() => route.query.tab === 'members' ? 'members' : 'capabilities')
</script>
<template><PageHeader title="安全治理" description="算子能力决定了能做什么，项目成员决定了谁有权做。" /><nav class="tabs"><RouterLink to="/governance" :class="{ selected: tab === 'capabilities' }">算子能力</RouterLink><RouterLink to="/governance?tab=members" :class="{ selected: tab === 'members' }">项目成员</RouterLink></nav><OperatorPanel v-if="tab === 'capabilities'" /><MemberPanel v-else /></template>
