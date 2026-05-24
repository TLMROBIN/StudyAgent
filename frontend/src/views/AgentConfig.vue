<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '../utils/api'

interface AgentConfigItem {
  id: number
  version: number
  system_prompt: string
  is_active: boolean
}

const configs = ref<AgentConfigItem[]>([])
const form = reactive({
  system_prompt: '',
})
const agentConfigCollectionPath = '/agent-config/'

async function loadConfigs() {
  const { data } = await api.get<AgentConfigItem[]>(agentConfigCollectionPath)
  configs.value = data
}

async function createConfig() {
  await api.post(agentConfigCollectionPath, {
    system_prompt: form.system_prompt,
    guidance_params: { fallback_after_turns: 3 },
    subject_prompts: {},
    filter_rules: {},
  })
  form.system_prompt = ''
  ElMessage.success('已创建新版本')
  await loadConfigs()
}

async function activateConfig(id: number) {
  await api.post(`/agent-config/${id}/activate`)
  ElMessage.success('已切换生效版本')
  await loadConfigs()
}

onMounted(async () => {
  await loadConfigs()
})
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Prompt Versioning</p>
          <h2>智能体配置</h2>
        </div>
      </div>
      <el-input
        v-model="form.system_prompt"
        type="textarea"
        :rows="8"
        resize="none"
        placeholder="输入新版系统提示词"
      />
      <button class="primary-button" @click="createConfig">创建新版本</button>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Version List</p>
          <h2>配置版本</h2>
        </div>
      </div>
      <article v-for="item in configs" :key="item.id" class="version-card">
        <div>
          <strong>v{{ item.version }}</strong>
          <span>{{ item.is_active ? '当前生效' : '未启用' }}</span>
        </div>
        <p>{{ item.system_prompt }}</p>
        <button class="ghost-button" :disabled="item.is_active" @click="activateConfig(item.id)">设为生效版本</button>
      </article>
    </section>
  </section>
</template>
