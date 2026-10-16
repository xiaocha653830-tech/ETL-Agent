<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import type { Datasource, Profile } from '../../types'
import { dateTime } from '../../utils/display'
import EmptyState from '../shared/EmptyState.vue'
const props = defineProps<{ source: Datasource; profiles: Profile[]; loading: boolean }>()
const profileId = shallowRef('')
const tableName = shallowRef('')
const profile = computed(() => props.profiles.find(item => item.id === profileId.value) ?? props.profiles[0])
const table = computed(() => profile.value?.schema_json.tables.find(item => item.name === tableName.value) ?? profile.value?.schema_json.tables[0])
</script>
<template>
  <div class="two-col two-col-left"><aside class="panel stack"><h2>{{ source.name }}</h2><span class="badge">{{ source.type }} / 只读探查</span><dl class="kv-list"><div class="kv"><dt>数据库</dt><dd>{{ source.config.database || '—' }}</dd></div><div class="kv"><dt>最高敏感级别</dt><dd>{{ profile?.max_level || '尚未探查' }}</dd></div><div class="kv"><dt>探查表数</dt><dd>{{ profile?.tables_scanned ?? '—' }}</dd></div></dl><hr class="divider" /><h3>凭据{{ source.has_credential ? '已安全保存' : '尚未配置' }}</h3><p class="small muted">不展示或回填原始密码。新增连接时填写凭据。</p></aside><section class="panel stack"><div class="panel-title"><h2>字段快照</h2><span v-if="profile" class="badge">{{ dateTime(profile.sampled_at) }}</span></div><EmptyState v-if="loading" title="加载探查记录" loading /><EmptyState v-else-if="!profile" title="尚无探查记录" description="工程师可发起只读探查，生成字段与脱敏样本快照。" /><template v-else><div class="field-grid"><label>探查历史<select v-model="profileId"><option value="">最新快照</option><option v-for="item in profiles" :key="item.id" :value="item.id">{{ dateTime(item.sampled_at) }}</option></select></label><label>数据表<select v-model="tableName"><option value="">选择数据表</option><option v-for="item in profile.schema_json.tables" :key="item.name" :value="item.name">{{ item.name }}</option></select></label></div><div v-if="table" class="table-wrap"><table><thead><tr><th>字段</th><th>类型</th><th>分级</th><th>脱敏样本</th></tr></thead><tbody><tr v-for="column in table.columns" :key="column.name"><td>{{ column.name }}</td><td>{{ column.type }}</td><td><span class="badge">{{ column.level }}</span></td><td>{{ column.samples.join(' / ') || '无样本' }}</td></tr></tbody></table></div><p class="small muted">探查记录会绑定生成版本；再次探查不会改写已冻结版本。</p></template></section></div>
</template>
