<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import type { Role, Run } from '../../types'
import { cancelRestriction, rollbackRestriction } from '../../utils/permissions'
import ConfirmDialog from '../shared/ConfirmDialog.vue'
const props = defineProps<{ run: Run; role?: Role; busy: boolean }>()
const emit = defineEmits<{ cancel: []; rollback: [] }>()
const confirmation = shallowRef(false)
const cancelReason = computed(() => cancelRestriction(props.run, props.role))
const rollbackReason = computed(() => rollbackRestriction(props.run, props.role))
function confirm() { if (!rollbackReason.value) { confirmation.value = false; emit('rollback') } }
</script>
<template><div class="stack"><section class="panel stack"><h2>运行操作</h2><template v-if="!cancelReason"><p class="small muted">取消是<strong>请求</strong>而非强杀：执行层会在安全点停下，界面上会先显示「正在停止」。</p><button class="btn" :disabled="busy" @click="emit('cancel')">请求取消</button></template><p v-else class="small muted">{{ cancelReason }}</p></section><section class="panel stack"><h2>清理残留</h2><p class="small muted">{{ rollbackReason || '丢弃未发布的影子表残留。正式表不受影响。' }}</p><button class="btn" :disabled="!!rollbackReason || busy" @click="confirmation = true">清理影子表残留</button><p class="small muted">删哪张表由已批准的准备单决定，不由操作方指定。</p></section><p class="small muted">最终权限以服务端校验为准。</p><ConfirmDialog :open="confirmation" title="清理这次运行的影子表残留？" confirm-label="确认清理" :busy="busy" @close="confirmation = false" @confirm="confirm"><p>影子表及其派生的错误表将被删除并释放存储。正式表保持不变。</p><div class="mono">{{ run.id }}</div><p v-if="rollbackReason">{{ rollbackReason }}</p></ConfirmDialog></div></template>
