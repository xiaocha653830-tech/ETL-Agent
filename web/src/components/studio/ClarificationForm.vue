<script setup lang="ts">
import { reactive } from 'vue'
import type { Generation } from '../../types'
defineProps<{ questions: Generation['questions']; busy: boolean; editable: boolean }>()
const emit = defineEmits<{ submit: [answers: Record<string, string>] }>()
const answers = reactive<Record<string, string>>({})
</script>
<template><form class="panel stack" @submit.prevent="emit('submit', { ...answers })"><div class="panel-title"><h2>补充需求</h2><span class="badge">{{ questions.length }} 个问题</span></div><p class="muted">已完成的步骤会保留。提交答案后从中断位置继续生成。</p><fieldset class="stack" :disabled="busy || !editable"><label v-for="(question, index) in questions" :key="question.key"><span class="question-title">{{ index + 1 }}. {{ question.question }}</span><span v-if="question.hint" class="field-help">{{ question.hint }}</span><select v-if="question.options.length" v-model="answers[question.key]" required><option value="" disabled>请选择</option><option v-for="option in question.options" :key="option" :value="option">{{ option }}</option></select><textarea v-else v-model="answers[question.key]" required rows="3" placeholder="请补充具体信息" /></label><button class="btn btn-primary" type="submit">{{ busy ? '正在继续生成…' : '提交答案并继续' }}</button></fieldset></form></template>
<style scoped>.question-title{font-size:15px;color:#000;font-weight:500;margin-bottom:5px}</style>
