<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '../utils/api'

interface AgentConfigItem {
  id: number
  version: number
  system_prompt: string
  is_active: boolean
}

interface LLMProviderItem {
  id: number
  name: string
  base_url: string
  model: string
  has_api_key: boolean
  is_active: boolean
  is_fallback: boolean
}

const configs = ref<AgentConfigItem[]>([])
const providers = ref<LLMProviderItem[]>([])
const providerLoading = ref(false)
const editingProviderId = ref<number | null>(null)
const form = reactive({
  system_prompt: '',
})
const providerForm = reactive({
  name: '',
  base_url: '',
  model: '',
  api_key: '',
})
const selectionForm = reactive<{
  active_provider_id: number | null
  fallback_provider_id: number | null
}>({
  active_provider_id: null,
  fallback_provider_id: null,
})
const agentConfigCollectionPath = '/agent-config/'
const llmProviderCollectionPath = '/llm-providers/'

const providerFormTitle = computed(() => (editingProviderId.value ? '编辑接入' : '新增接入'))
const providerSubmitText = computed(() => (editingProviderId.value ? '保存修改' : '保存接入'))

async function loadConfigs() {
  const { data } = await api.get<AgentConfigItem[]>(agentConfigCollectionPath)
  configs.value = data
}

async function loadProviders() {
  providerLoading.value = true
  try {
    const { data } = await api.get<LLMProviderItem[]>(llmProviderCollectionPath)
    providers.value = data
    selectionForm.active_provider_id = data.find((item) => item.is_active)?.id ?? null
    selectionForm.fallback_provider_id = data.find((item) => item.is_fallback)?.id ?? null
  } catch (error) {
    console.error(error)
    ElMessage.error('大模型接入配置加载失败')
  } finally {
    providerLoading.value = false
  }
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

function resetProviderForm() {
  editingProviderId.value = null
  providerForm.name = ''
  providerForm.base_url = ''
  providerForm.model = ''
  providerForm.api_key = ''
}

function editProvider(item: LLMProviderItem) {
  editingProviderId.value = item.id
  providerForm.name = item.name
  providerForm.base_url = item.base_url
  providerForm.model = item.model
  providerForm.api_key = ''
}

async function saveProvider() {
  if (!providerForm.name.trim() || !providerForm.base_url.trim() || !providerForm.model.trim()) {
    ElMessage.warning('请填写名称、Base URL 和模型名')
    return
  }
  if (!editingProviderId.value && !providerForm.api_key.trim()) {
    ElMessage.warning('新增接入需要填写 API Key')
    return
  }

  const payload: {
    name: string
    base_url: string
    model: string
    api_key?: string
  } = {
    name: providerForm.name.trim(),
    base_url: providerForm.base_url.trim(),
    model: providerForm.model.trim(),
  }
  if (providerForm.api_key.trim()) {
    payload.api_key = providerForm.api_key.trim()
  }

  if (editingProviderId.value) {
    await api.put(`/llm-providers/${editingProviderId.value}`, payload)
    ElMessage.success('已更新模型接入')
  } else {
    await api.post(llmProviderCollectionPath, payload)
    ElMessage.success('已保存模型接入')
  }
  resetProviderForm()
  await loadProviders()
}

async function saveSelection() {
  if (!selectionForm.active_provider_id) {
    ElMessage.warning('请选择主模型')
    return
  }
  if (selectionForm.fallback_provider_id && selectionForm.fallback_provider_id === selectionForm.active_provider_id) {
    ElMessage.warning('备用模型不能与主模型相同')
    return
  }
  await api.post('/llm-providers/selection', {
    active_provider_id: selectionForm.active_provider_id,
    fallback_provider_id: selectionForm.fallback_provider_id,
  })
  ElMessage.success('已切换大模型')
  await loadProviders()
}

async function activateConfig(id: number) {
  await api.post(`/agent-config/${id}/activate`)
  ElMessage.success('已切换生效版本')
  await loadConfigs()
}

onMounted(async () => {
  await Promise.all([loadConfigs(), loadProviders()])
})
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Model Providers</p>
          <h2>大模型 API 接入</h2>
        </div>
        <div class="toolbar">
          <button class="ghost-button" :disabled="providerLoading" @click="loadProviders">刷新</button>
        </div>
      </div>

      <div class="llm-layout">
        <section class="llm-editor">
          <div class="panel-header panel-header--compact">
            <div>
              <p class="eyebrow">{{ providerFormTitle }}</p>
              <h3>OpenAI 兼容接口</h3>
            </div>
          </div>
          <div class="llm-form-grid">
            <el-input v-model="providerForm.name" placeholder="显示名称，如 MiniMax / Qwen" />
            <el-input v-model="providerForm.model" placeholder="模型名，如 MiniMax-M2.7-highspeed" />
            <el-input v-model="providerForm.base_url" placeholder="Base URL，如 https://api.example.com/v1" />
            <el-input
              v-model="providerForm.api_key"
              type="password"
              show-password
              :placeholder="editingProviderId ? '留空则保留原 API Key' : 'API Key'"
            />
          </div>
          <div class="toolbar">
            <button class="primary-button" @click="saveProvider">{{ providerSubmitText }}</button>
            <button v-if="editingProviderId" class="ghost-button" @click="resetProviderForm">取消编辑</button>
          </div>
        </section>

        <section class="llm-editor">
          <div class="panel-header panel-header--compact">
            <div>
              <p class="eyebrow">Routing</p>
              <h3>模型选择</h3>
            </div>
          </div>
          <div class="llm-form-grid">
            <el-select v-model="selectionForm.active_provider_id" placeholder="选择主模型">
              <el-option v-for="item in providers" :key="item.id" :label="`${item.name} · ${item.model}`" :value="item.id" />
            </el-select>
            <el-select v-model="selectionForm.fallback_provider_id" placeholder="选择备用模型（可选）" clearable>
              <el-option
                v-for="item in providers"
                :key="item.id"
                :disabled="item.id === selectionForm.active_provider_id"
                :label="`${item.name} · ${item.model}`"
                :value="item.id"
              />
            </el-select>
          </div>
          <button class="primary-button" :disabled="!providers.length" @click="saveSelection">应用选择</button>
        </section>
      </div>

      <div class="table-like llm-provider-list">
        <article v-for="item in providers" :key="item.id" class="table-row table-row-wrap">
          <div class="table-main">
            <strong>{{ item.name }}</strong>
            <span>{{ item.model }} · {{ item.base_url }}</span>
          </div>
          <div class="detail-chip-group">
            <span v-if="item.is_active" class="detail-chip detail-chip--success">主模型</span>
            <span v-if="item.is_fallback" class="detail-chip">备用</span>
            <span class="detail-chip">{{ item.has_api_key ? '密钥已保存' : '缺少密钥' }}</span>
            <button class="ghost-button" @click="editProvider(item)">编辑</button>
          </div>
        </article>
        <p v-if="!providers.length" class="panel-subcopy">暂无模型接入配置；未配置时后端继续使用环境变量中的 LLM 设置。</p>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Prompt Versioning</p>
          <h2>智能体配置管理</h2>
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

<style scoped>
.llm-layout {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.llm-editor {
  display: grid;
  gap: 14px;
  min-width: 0;
}

.panel-header--compact {
  margin-bottom: 0;
}

.panel-header--compact h3 {
  margin: 0;
  font-size: 1rem;
}

.llm-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.llm-form-grid .el-input:nth-child(3),
.llm-form-grid .el-input:nth-child(4) {
  grid-column: 1 / -1;
}

.llm-provider-list {
  margin-top: 18px;
}

.detail-chip--success {
  border-color: rgba(69, 140, 92, 0.35);
  background: rgba(215, 239, 220, 0.72);
}

@media (max-width: 860px) {
  .llm-layout,
  .llm-form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
