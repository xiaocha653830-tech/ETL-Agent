<script setup lang="ts">
import { shallowRef, watch } from 'vue'
import type { Member, Role } from '../../types'
import { roleLabels } from '../../utils/display'
const props = defineProps<{ member: Member; manage: boolean; busy: boolean }>()
const emit = defineEmits<{ update: [role: Role]; remove: [] }>()
const role = shallowRef<Role>(props.member.role)
watch(() => props.member.role, value => { role.value = value })
</script>
<template><tr><td>{{ member.display_name || member.email }}<span class="sub">{{ member.email }}</span></td><td><select v-if="manage" v-model="role" :aria-label="`${member.email} 的角色`" :disabled="busy"><option v-for="(label, value) in roleLabels" :key="value" :value="value">{{ label }}</option></select><span v-else>{{ roleLabels[member.role] }}</span></td><td v-if="manage"><div class="actions"><button class="btn btn-small" :disabled="busy || role === member.role" @click="emit('update', role)">保存角色</button><button class="text-button" :disabled="busy" @click="emit('remove')">移出项目</button></div></td></tr></template>
