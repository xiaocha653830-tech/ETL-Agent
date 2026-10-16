<script setup lang="ts">
import { CircleAlert, CheckCircle2, Info } from 'lucide-vue-next'
defineProps<{ error?: string; success?: string; note?: string }>()
</script>
<template>
  <div v-if="error" class="notice notice-error" role="alert"><CircleAlert :size="19" /><div><strong>操作未完成</strong><p>{{ error }}</p></div></div>
  <div v-else-if="success" class="notice" role="status"><CheckCircle2 :size="19" /><p>{{ success }}</p></div>
  <div v-else-if="note" class="notice"><Info :size="19" /><p>{{ note }}</p></div>
</template>
