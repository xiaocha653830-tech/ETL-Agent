<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import type { Preparation, Role } from '../../types'
import { useNow } from '../../composables/useNow'
import { approvalRestriction, commitRestriction } from '../../utils/permissions'
import StatusBadge from '../shared/StatusBadge.vue'
import ConfirmDialog from '../shared/ConfirmDialog.vue'
const props = defineProps<{ preparation: Preparation; role?: Role; userId: string; busy: boolean; names: Record<string, string> }>()
const emit = defineEmits<{ decide: [approvalId: string, decision: 'approved' | 'rejected', comment: string]; commit: [] }>()
const now = useNow()
const comment = shallowRef('')
const confirmation = shallowRef(false)
const approvalReason = computed(() => approvalRestriction(props.preparation, props.userId, props.role, now.value))
const commitReason = computed(() => commitRestriction(props.preparation, props.role, now.value))
const myDimension = computed(() => props.role === 'checker_data' ? 'data' : 'security')
const slot = computed(() => props.preparation.approvals.find(item => item.dimension === myDimension.value))
function decide(decision: 'approved' | 'rejected') { if (!approvalReason.value && slot.value) emit('decide', slot.value.id, decision, comment.value) }
function commit() { if (!commitReason.value) { confirmation.value = false; emit('commit') } }
</script>
<template><div class="stack"><section class="panel stack"><h2>双维独立审批</h2><article v-for="item in preparation.approvals" :key="item.id" class="approval-slot"><div class="inline spread"><strong>{{ item.dimension === 'data' ? '数据审批' : '安全审批' }}</strong><StatusBadge :status="item.status" /></div><p class="small muted">{{ item.approver_id ? names[item.approver_id] || item.approver_id : '尚未处理' }}</p><p v-if="item.comment" class="small">{{ item.comment }}</p></article><template v-if="!approvalReason"><label>审批意见<textarea v-model="comment" maxlength="512" rows="3" placeholder="请输入审批依据或拒绝原因" :disabled="busy" /></label><div class="actions"><button class="btn" :disabled="busy" @click="decide('rejected')">拒绝</button><button class="btn btn-primary" :disabled="busy" @click="decide('approved')">批准{{ myDimension === 'data' ? '数据' : '安全' }}维度</button></div></template><p v-else class="small muted">{{ approvalReason }}</p><p class="small muted">申请人不能审批自己的方案；同一人不能占用两个审批维度。最终权限以服务端校验为准。</p></section><section class="panel stack"><h2>提交执行</h2><p class="small muted">{{ commitReason || '双维审批已完成。提交后产生新的运行，由执行层处理。' }}</p><button class="btn btn-primary" :disabled="!!commitReason || busy" @click="confirmation = true">提交执行</button><RouterLink v-if="preparation.status === 'committed'" to="/runs" class="text-button">查看执行记录</RouterLink><p class="small muted">提交前会再次复核版本指纹与审批有效期。</p></section></div><ConfirmDialog :open="confirmation" title="提交这个冻结版本？" confirm-label="确认提交执行" :busy="busy" @close="confirmation = false" @confirm="commit"><p>提交后执行层将按已批准的影响范围与预算启动数据作业。</p><div class="mono">{{ preparation.fingerprint }}</div><p v-if="commitReason">{{ commitReason }}</p></ConfirmDialog></template>
<style scoped>.approval-slot{display:flex;flex-direction:column;gap:8px;padding-bottom:14px;border-bottom:1px solid var(--line)}.approval-slot strong{font-size:13px}</style>
