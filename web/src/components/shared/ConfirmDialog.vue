<script setup lang="ts">
import { nextTick, watch, useTemplateRef } from 'vue'
import { X, TriangleAlert } from 'lucide-vue-next'
const props = withDefaults(defineProps<{ open: boolean; title: string; confirmLabel?: string; busy?: boolean }>(), { confirmLabel: '确认操作' })
const emit = defineEmits<{ close: []; confirm: [] }>()
const dialog = useTemplateRef<HTMLDialogElement>('dialog')
watch(() => props.open, async (open) => { await nextTick(); if (open && !dialog.value?.open) dialog.value?.showModal(); else if (!open) dialog.value?.close() }, { immediate: true })
</script>
<template>
  <dialog ref="dialog" class="dialog" aria-labelledby="confirmation-title" @cancel.prevent="!busy && emit('close')">
    <div class="dialog-header"><TriangleAlert :size="25" /><button class="icon-button" aria-label="关闭确认框" :disabled="busy" @click="emit('close')"><X :size="20" /></button></div>
    <h2 id="confirmation-title">{{ title }}</h2><div class="dialog-content"><slot /></div>
    <footer class="actions"><button class="btn" :disabled="busy" @click="emit('close')">返回</button><button class="btn btn-primary" :disabled="busy" @click="emit('confirm')">{{ busy ? '处理中…' : confirmLabel }}</button></footer>
  </dialog>
</template>
