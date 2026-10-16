<script setup lang="ts">
import { Plus } from 'lucide-vue-next'
import PageHeader from '../components/shared/PageHeader.vue'
import DashboardOverview from '../components/dashboard/DashboardOverview.vue'
import { useSession } from '../composables/useSession'
const { role } = useSession()
</script>
<template><PageHeader title="项目总览" description="从连接与方案开始，跟进每一次审批和执行。"><RouterLink v-if="role === 'maker'" to="/studio" class="btn btn-primary"><Plus :size="16" />新建数据方案</RouterLink></PageHeader><nav class="tabs"><RouterLink to="/dashboard" class="selected">总览</RouterLink><RouterLink to="/datasources">数据源</RouterLink><RouterLink to="/governance?tab=members">项目成员</RouterLink></nav><DashboardOverview /></template>
