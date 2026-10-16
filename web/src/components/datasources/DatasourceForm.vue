<script setup lang="ts">
import { computed, reactive } from 'vue'
defineProps<{ busy: boolean }>()
const emit = defineEmits<{ submit: [body: { name: string; type: string; config: Record<string, unknown>; credential: Record<string, string> }]; close: [] }>()
const form = reactive({ name: '', type: 'mysql', host: '127.0.0.1', port: 3306, database: '', username: '', password: '', url: '', fenodes: '' })
const isDoris = computed(() => form.type === 'doris')
function changeType() { form.port = form.type === 'postgresql' ? 5432 : form.type === 'doris' ? 9030 : 3306 }
function submit() {
  const config: Record<string, unknown> = { host: form.host.trim(), port: form.port, database: form.database.trim() }
  if (form.url.trim()) config.url = form.url.trim()
  if (isDoris.value) { config.sql_port = form.port; config.fenodes = form.fenodes.trim() }
  emit('submit', { name: form.name.trim(), type: form.type, config, credential: { username: form.username, password: form.password } })
}
</script>
<template>
  <form class="panel stack" @submit.prevent="submit"><div class="panel-title"><h2>登记数据源</h2><button type="button" class="text-button" :disabled="busy" @click="emit('close')">收起</button></div><fieldset :disabled="busy" class="field-grid"><label>名称<input v-model="form.name" required maxlength="128" placeholder="订单源库" /></label><label>类型<select v-model="form.type" @change="changeType"><option value="mysql">MySQL</option><option value="postgresql">PostgreSQL</option><option value="doris">Doris</option></select></label><label>数据库主机<input v-model="form.host" required placeholder="127.0.0.1" /></label><label>SQL 端口<input v-model.number="form.port" type="number" min="1" max="65535" required /></label><label>数据库<input v-model="form.database" required placeholder="demo" /></label><label>用户名<input v-model="form.username" required autocomplete="off" /></label><label>密码<input v-model="form.password" type="password" autocomplete="new-password" /><span class="field-help">允许空密码；凭据仅提交一次，不写入浏览器存储。</span></label><label v-if="isDoris">Doris FE HTTP 地址<input v-model="form.fenodes" required placeholder="etl-doris-fe:8030" /><span class="field-help">由 SeaTunnel 容器访问，区别于 SQL 端口。</span></label><label v-else>引擎 JDBC 地址（可选）<input v-model="form.url" placeholder="jdbc:mysql://host.docker.internal:3307/demo" /><span class="field-help">引擎与控制面处于不同网络时配置。</span></label></fieldset><div class="actions"><button class="btn btn-primary" :disabled="busy">{{ busy ? '正在登记…' : '保存连接' }}</button><span class="small muted">保存后单独测试连接和只读探查。</span></div></form>
</template>
