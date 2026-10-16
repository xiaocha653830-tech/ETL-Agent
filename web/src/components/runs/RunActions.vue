<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import type { Role, Run, Preparation, RollbackResult } from '../../types'
import { rollbackRestriction } from '../../utils/permissions'
import { terminal } from '../../utils/display'
import ConfirmDialog from '../shared/ConfirmDialog.vue'
const props = defineProps<{ run: Run; role?: Role; busy: boolean; preparation: Preparation | null; result: RollbackResult | null }>()
const emit = defineEmits<{ cancel: []; rollback: [] }>()
const confirmation = shallowRef<'cancel' | 'rollback' | null>(null)
const restriction = computed(() => rollbackRestriction(props.run, props.role))
const canCancel = computed(() => props.role === 'operator' && !terminal(props.run.status) && !props.run.cancel_requested)
function confirm() { const operation = confirmation.value; confirmation.value = null; if (operation === 'cancel' && canCancel.value) emit('cancel'); else if (operation === 'rollback' && !restriction.value) emit('rollback') }
</script>
<template><section class="stack"><div class="actions"><button v-if="role === 'operator' && !terminal(run.status)" class="btn" :disabled="busy || !canCancel" @click="confirmation = 'cancel'">{{ run.cancel_requested ? '正在停止…' : '请求取消' }}</button><button v-if="role === 'operator' && terminal(run.status)" class="btn btn-danger" :disabled="busy || !!restriction" @click="confirmation = 'rollback'">{{ run.rolled_back_at ? '查看既有清理结果' : '清理未发布残留' }}</button><RouterLink :to="`/versions/${run.version_id}`" class="btn">查看冻结版本</RouterLink></div><p v-if="restriction && terminal(run.status)" class="small muted">{{ restriction }}</p><p v-if="run.cancel_requested && !terminal(run.status)" class="small muted">取消请求已发出；只有运行进入取消终态才显示“已取消”。</p><div v-if="result" class="notice" role="status"><div><strong>{{ result.executed ? '本次清理已执行' : '清理结果' }}</strong><p>{{ result.reason }}</p><p v-if="result.dropped.length" class="mono">{{ result.dropped.join('\n') }}</p></div></div></section><ConfirmDialog :open="confirmation !== null" :title="confirmation === 'cancel' ? '请求停止本次运行？' : '清理本次运行残留？'" :confirm-label="confirmation === 'cancel' ? '确认请求取消' : '确认清理'" :busy="busy" @close="confirmation = null" @confirm="confirm"><template v-if="confirmation === 'cancel'"><p>请求将由持有运行租约的执行层处理。返回运行中状态并不代表取消失败，请等待终态。</p></template><template v-else><p>只清理服务端认定的未发布影子表与错误表，不撤销已生效的发布。</p><div v-if="preparation" class="code-block">{{ preparation.impact_scope.shadow_table }}</div><p class="small muted">表名由准备单绑定，不能手动修改。同一版本已有发布记录时，服务端会拒绝清理，以保护旧正式表。</p></template></ConfirmDialog></template>
