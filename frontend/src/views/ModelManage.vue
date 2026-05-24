<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  createLLMModelConfig,
  createLLMProviderAccount,
  deleteLLMModelConfig,
  fetchLLMModelConfigs,
  fetchLLMProviderAccounts,
  fetchLLMUsageSummary,
  type LLMModelConfig,
  type LLMModelConfigPayload,
  type LLMProviderAccount,
  type LLMUsageSummary,
  updateLLMModelConfig,
} from '../utils/api'

const accounts = ref<LLMProviderAccount[]>([])
const models = ref<LLMModelConfig[]>([])
const usage = ref<LLMUsageSummary[]>([])
const loading = ref(false)
const editingModelId = ref<number | null>(null)

const accountForm = reactive({
  provider_name: 'minimax',
  display_name: 'MiniMax Token Plan',
  base_url: 'https://api.minimax.chat/v1',
  api_key: '',
  account_billing_type: 'token_plan' as 'token_plan' | 'pay_as_you_go' | 'local',
  is_enabled: true,
})

const modelForm = reactive({
  model_key: 'minimax-m27',
  display_name: 'MiniMax M2.7',
  description: '高速答疑模型',
  provider_account_id: 0,
  provider_model: 'MiniMax-M2.7-highspeed',
  billing_mode: 'request_count' as 'request_count' | 'token_usage' | 'free_local',
  user_daily_request_limit: 20,
  user_daily_token_limit: 50000,
  max_completion_tokens: 1024,
  provider_rolling_5h_request_limit: null as number | null,
  provider_weekly_request_limit: null as number | null,
  count_cache_hit: false,
  is_primary: true,
  is_fallback: false,
  is_enabled: true,
})

const canCreateModel = computed(() => accounts.value.length > 0 && modelForm.provider_account_id > 0)
const modelSubmitText = computed(() => (editingModelId.value ? '保存模型' : '创建模型'))

async function loadData() {
  loading.value = true
  try {
    const [nextAccounts, nextModels, nextUsage] = await Promise.all([
      fetchLLMProviderAccounts(),
      fetchLLMModelConfigs(),
      fetchLLMUsageSummary().catch(() => []),
    ])
    accounts.value = nextAccounts
    models.value = nextModels
    usage.value = nextUsage
    if (!modelForm.provider_account_id && nextAccounts[0]) {
      modelForm.provider_account_id = nextAccounts[0].id
    }
  } finally {
    loading.value = false
  }
}

function resetModelForm() {
  editingModelId.value = null
  modelForm.model_key = 'minimax-m27'
  modelForm.display_name = 'MiniMax M2.7'
  modelForm.description = '高速答疑模型'
  modelForm.provider_account_id = accounts.value[0]?.id ?? 0
  modelForm.provider_model = 'MiniMax-M2.7-highspeed'
  modelForm.billing_mode = 'request_count'
  modelForm.user_daily_request_limit = 20
  modelForm.user_daily_token_limit = 50000
  modelForm.max_completion_tokens = 1024
  modelForm.provider_rolling_5h_request_limit = null
  modelForm.provider_weekly_request_limit = null
  modelForm.count_cache_hit = false
  modelForm.is_primary = true
  modelForm.is_fallback = false
  modelForm.is_enabled = true
}

function editModel(item: LLMModelConfig) {
  editingModelId.value = item.id
  modelForm.model_key = item.model_key
  modelForm.display_name = item.display_name
  modelForm.description = item.description
  modelForm.provider_account_id = item.provider_account_id
  modelForm.provider_model = item.provider_model
  modelForm.billing_mode = item.quota_policy.billing_mode
  modelForm.user_daily_request_limit = item.quota_policy.user_daily_request_limit ?? 20
  modelForm.user_daily_token_limit = item.quota_policy.user_daily_token_limit ?? 50000
  modelForm.max_completion_tokens = item.quota_policy.max_completion_tokens ?? 1024
  modelForm.provider_rolling_5h_request_limit = item.quota_policy.provider_rolling_5h_request_limit
  modelForm.provider_weekly_request_limit = item.quota_policy.provider_weekly_request_limit
  modelForm.count_cache_hit = item.quota_policy.count_cache_hit
  modelForm.is_primary = item.is_primary
  modelForm.is_fallback = item.is_fallback
  modelForm.is_enabled = item.is_enabled
}

function buildModelPayload(): LLMModelConfigPayload {
  return {
    model_key: modelForm.model_key,
    display_name: modelForm.display_name,
    description: modelForm.description,
    provider_account_id: modelForm.provider_account_id,
    provider_model: modelForm.provider_model,
    capability_text: true,
    capability_vision: false,
    is_enabled: modelForm.is_enabled,
    is_primary: modelForm.is_primary,
    is_fallback: modelForm.is_fallback,
    sort_order: modelForm.is_primary ? 10 : 100,
    quota_policy: {
      billing_mode: modelForm.billing_mode,
      user_daily_request_limit: modelForm.billing_mode === 'request_count' ? modelForm.user_daily_request_limit : null,
      user_daily_token_limit: modelForm.billing_mode === 'token_usage' ? modelForm.user_daily_token_limit : null,
      max_completion_tokens: modelForm.billing_mode === 'token_usage' ? modelForm.max_completion_tokens : null,
      provider_rolling_5h_request_limit: modelForm.billing_mode === 'request_count' ? modelForm.provider_rolling_5h_request_limit : null,
      provider_weekly_request_limit: modelForm.billing_mode === 'request_count' ? modelForm.provider_weekly_request_limit : null,
      count_cache_hit: modelForm.count_cache_hit,
      fail_closed_on_store_error: true,
    },
  }
}

async function submitAccount() {
  if (!accountForm.api_key.trim()) {
    ElMessage.error('请填写 API Key')
    return
  }
  await createLLMProviderAccount({ ...accountForm })
  ElMessage.success('供应商账户已创建')
  accountForm.api_key = ''
  await loadData()
}

async function submitModel() {
  if (!canCreateModel.value) {
    ElMessage.error('请先创建供应商账户')
    return
  }
  const payload = buildModelPayload()
  if (editingModelId.value) {
    await updateLLMModelConfig(editingModelId.value, payload)
    ElMessage.success('模型配置已更新')
  } else {
    await createLLMModelConfig(payload)
    ElMessage.success('模型配置已创建')
  }
  resetModelForm()
  await loadData()
}

async function deleteModel(item: LLMModelConfig) {
  try {
    await ElMessageBox.confirm(`确认删除模型 ${item.display_name}（${item.model_key}）？`, '删除模型配置', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }

  await deleteLLMModelConfig(item.id)
  if (editingModelId.value === item.id) {
    resetModelForm()
  }
  ElMessage.success('模型配置已删除')
  await loadData()
}

onMounted(() => {
  void loadData()
})
</script>

<template>
  <section class="dashboard-stack">
    <div class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">LLM Control</p>
          <h2>模型设置</h2>
        </div>
        <button class="ghost-button" :disabled="loading" @click="loadData">刷新</button>
      </div>

      <div class="model-admin-grid">
        <form class="model-admin-form" @submit.prevent="submitAccount">
          <h3>供应商账户</h3>
          <el-input v-model="accountForm.display_name" placeholder="显示名称" />
          <el-input v-model="accountForm.provider_name" placeholder="供应商标识" />
          <el-input v-model="accountForm.base_url" placeholder="Base URL" />
          <el-input v-model="accountForm.api_key" type="password" show-password placeholder="API Key" />
          <el-select v-model="accountForm.account_billing_type">
            <el-option label="Token Plan" value="token_plan" />
            <el-option label="按量付费" value="pay_as_you_go" />
            <el-option label="本地模型" value="local" />
          </el-select>
          <el-switch v-model="accountForm.is_enabled" active-text="启用" />
          <button class="primary-button" type="submit">创建账户</button>
        </form>

        <form class="model-admin-form" @submit.prevent="submitModel">
          <div class="model-form-title">
            <h3>{{ editingModelId ? '编辑模型配置' : '模型配置' }}</h3>
            <button v-if="editingModelId" class="ghost-button" type="button" @click="resetModelForm">取消编辑</button>
          </div>
          <el-select v-model="modelForm.provider_account_id" placeholder="供应商账户">
            <el-option v-for="account in accounts" :key="account.id" :label="account.display_name" :value="account.id" />
          </el-select>
          <el-input v-model="modelForm.model_key" placeholder="模型 Key" />
          <el-input v-model="modelForm.display_name" placeholder="显示名称" />
          <el-input v-model="modelForm.provider_model" placeholder="上游模型名" />
          <el-input v-model="modelForm.description" placeholder="描述" />
          <el-select v-model="modelForm.billing_mode">
            <el-option label="按请求次数" value="request_count" />
            <el-option label="按 token" value="token_usage" />
            <el-option label="本地免费" value="free_local" />
          </el-select>
          <template v-if="modelForm.billing_mode === 'request_count'">
            <el-input-number v-model="modelForm.user_daily_request_limit" :min="1" controls-position="right" />
            <el-input-number v-model="modelForm.provider_rolling_5h_request_limit" :min="1" controls-position="right" placeholder="供应商 5 小时额度" />
            <el-input-number v-model="modelForm.provider_weekly_request_limit" :min="1" controls-position="right" placeholder="供应商周额度" />
          </template>
          <template v-if="modelForm.billing_mode === 'token_usage'">
            <el-input-number v-model="modelForm.user_daily_token_limit" :min="1" controls-position="right" />
            <el-input-number v-model="modelForm.max_completion_tokens" :min="1" controls-position="right" />
          </template>
          <el-switch v-model="modelForm.count_cache_hit" active-text="缓存命中计入用户额度" />
          <el-switch v-model="modelForm.is_primary" active-text="主模型" />
          <el-switch v-model="modelForm.is_fallback" active-text="备用模型" />
          <el-switch v-model="modelForm.is_enabled" active-text="启用" />
          <button class="primary-button" type="submit" :disabled="!canCreateModel">{{ modelSubmitText }}</button>
        </form>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h2>已配置模型</h2>
      </div>
      <el-table :data="models" stripe>
        <el-table-column prop="model_key" label="Key" min-width="140" />
        <el-table-column prop="display_name" label="名称" min-width="160" />
        <el-table-column prop="provider_model" label="上游模型" min-width="180" />
        <el-table-column prop="quota_policy.billing_mode" label="计费模式" width="120" />
        <el-table-column prop="quota_policy.user_daily_request_limit" label="每日次数" width="120" />
        <el-table-column prop="quota_policy.user_daily_token_limit" label="每日 tokens" width="130" />
        <el-table-column label="状态" width="160">
          <template #default="{ row }">
            <div class="detail-chip-group">
              <span v-if="row.is_primary" class="detail-chip detail-chip--success">主模型</span>
              <span v-if="row.is_fallback" class="detail-chip">备用</span>
              <span v-if="!row.is_enabled" class="detail-chip">停用</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" fixed="right" width="150">
          <template #default="{ row }">
            <div class="table-action-group">
              <button class="ghost-button" type="button" @click="editModel(row)">编辑</button>
              <button class="ghost-button danger-text" type="button" @click="deleteModel(row)">删除</button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h2>今日用量</h2>
      </div>
      <el-table :data="usage" stripe>
        <el-table-column prop="model_key" label="模型" min-width="140" />
        <el-table-column prop="billing_mode" label="模式" width="120" />
        <el-table-column prop="request_count" label="请求数" width="120" />
        <el-table-column prop="total_tokens" label="Tokens" width="140" />
      </el-table>
    </div>
  </section>
</template>
