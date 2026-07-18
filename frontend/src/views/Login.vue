<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchOidcLoginEnabled } from '../utils/oidc'

const oidcLoginEnabled = ref(false)

onMounted(async () => {
  oidcLoginEnabled.value = await fetchOidcLoginEnabled()
})

function loginWithSso() {
  window.location.href = `${import.meta.env.BASE_URL}api/auth/oidc/login`
}
</script>

<template>
  <section class="login-page">
    <div class="login-hero">
      <p class="eyebrow">LAN Classroom AI</p>
      <h1>让答疑更像一场耐心的推理训练</h1>
      <p>
        StudyAgent 你的专属苏格拉底式导师，结合知识库与多轮引导，帮助你一步步形成思路，锻炼思维能力，建立完整的知识体系。
      </p>
      <ul class="feature-list">
        <li>不限量问答，可在对话中请求类似练习，优先匹配知识点、年级与难易度。</li>
        <li>面向平板设备设计，适配局域网部署，不依赖外部网络。</li>
      </ul>
      <p class="login-note login-note--hero">目前仅具备物理知识库与题库，其他学科等待完善中</p>
    </div>
    <div class="login-card">
      <div class="login-card-head">
        <p class="eyebrow">Secure Access</p>
        <h2>统一认证登录</h2>
        <p>请使用学校统一账号登录，登录后可继续使用答疑、知识库与管理功能。</p>
      </div>
      <button
        v-if="oidcLoginEnabled"
        class="primary-button login-submit"
        type="button"
        @click="loginWithSso"
      >
        使用统一平台登录
      </button>
      <p v-else class="login-note">统一平台登录暂未启用，请联系管理员。</p>
    </div>
  </section>
</template>
