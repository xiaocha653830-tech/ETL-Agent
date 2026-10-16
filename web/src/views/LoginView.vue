<script setup lang="ts">
import AuthForm from '../components/auth/AuthForm.vue'
</script>
<template>
  <main class="login-page" id="main-content"><section class="login-brand"><a class="brand" href="/login">ETL-Agent</a><div class="login-story"><h2>把数据流转，<br />变成可控的交付。</h2><p>从一句需求，到一次经过审批的数据发布。<br />连接数据、冻结方案，让每次执行都有依据。</p><ol><li v-for="(item, index) in ['描述需求', '冻结版本', '独立审批', '受管发布']" :key="item"><span>0{{ index + 1 }}</span>{{ item }}</li></ol></div><footer>MySQL / SeaTunnel / Doris</footer></section><section class="login-fields"><AuthForm /></section></main>
</template>
<style scoped>
.login-page{min-height:100dvh;display:grid;grid-template-columns:43% 57%;background:white}.login-brand{background:#000;color:#fff;padding:48px 56px;display:flex;flex-direction:column;gap:40px}.login-story{margin:auto 0;max-width:490px}.login-story h2{font-size:clamp(30px,3vw,44px);font-weight:500;line-height:1.45;letter-spacing:-1px}.login-story p{color:#bdbdbd;font-size:15px;line-height:1.9;margin-top:22px}.login-story ol{list-style:none;padding:20px 0 0;margin:0;max-width:420px}.login-story li{display:flex;gap:22px;padding:17px 0;border-bottom:1px solid #353535;font-size:16px}.login-story li span{color:#999;font-size:12px;padding-top:3px}.login-brand footer{font-size:12px;color:#aaa}.login-fields{padding:50px;display:flex;align-items:center;justify-content:center}@media(max-width:850px){.login-brand{padding:35px}.login-fields{padding:30px}.login-story p br{display:none}}@media(max-width:650px){.login-page{grid-template-columns:1fr}.login-brand{padding:24px}.login-story,.login-brand footer{display:none}.login-fields{padding:40px 24px 60px}}
</style>
