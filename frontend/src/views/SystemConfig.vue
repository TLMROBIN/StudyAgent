<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { fetchSystemConfig, updateSystemConfig, type SystemConfigItem } from '../utils/api'

const items = ref<SystemConfigItem[]>([])
const loading = ref(false)
const saving = ref(false)
// 表单值：secret 项留空表示“不覆盖”
const form = reactive<Record<string, string>>({})

const SOURCE_LABELS: Record<string, string> = {
  db: '数据库（页面配置）',
  env: '环境变量',
  default: '默认值',
}

async function load() {
  loading.value = true
  try {
    const data = await fetchSystemConfig()
    items.value = data
    for (const item of data) {
      form[item.key] = item.secret ? '' : String(item.value ?? '')
    }
  } finally {
    loading.value = false
  }
}

async function save() {
  const payload: Record<string, string | null> = {}
  for (const item of items.value) {
    const value = (form[item.key] ?? '').trim()
    if (item.secret) {
      // secret 留空不覆盖；填了才提交
      if (value) {
        payload[item.key] = value
      }
      continue
    }
    payload[item.key] = value
  }
  saving.value = true
  try {
    const changed = await updateSystemConfig(payload)
    const keys = Object.keys(changed)
    ElMessage.success(keys.length ? `已保存 ${keys.length} 项配置，即时生效` : '没有需要保存的变更')
    await load()
  } catch (error: any) {
    ElMessage.error(error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

function resetSecret(key: string) {
  form[key] = ''
}

onMounted(load)
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">System Config</p>
          <h2>系统参数</h2>
          <p class="panel-subcopy">
            读取优先级：页面配置（数据库）→ 环境变量 → 默认值。保存后即时生效，无需重启服务。
            密钥项加密存储，仅显示掩码；留空表示不修改。
          </p>
        </div>
      </div>

      <el-skeleton v-if="loading" :rows="6" animated />
      <template v-else>
        <div v-for="item in items" :key="item.key" class="config-item">
          <div class="config-item-head">
            <div>
              <strong class="config-key">{{ item.key }}</strong>
              <span class="config-source" :class="`config-source--${item.source}`">
                当前来源：{{ SOURCE_LABELS[item.source] || item.source }}
              </span>
            </div>
            <p class="config-desc">{{ item.description }}</p>
          </div>

          <el-select
            v-if="item.type === 'enum'"
            v-model="form[item.key]"
            class="config-input"
          >
            <el-option
              v-for="choice in item.choices || []"
              :key="choice"
              :label="choice"
              :value="choice"
            />
          </el-select>

          <el-input
            v-else-if="item.type === 'int'"
            v-model="form[item.key]"
            class="config-input"
            type="number"
            :placeholder="`默认值：${item.default}`"
          />

          <div v-else-if="item.secret" class="config-secret">
            <el-input
              v-model="form[item.key]"
              class="config-input"
              type="password"
              show-password
              :placeholder="item.has_value ? `当前已配置：${item.value}（留空则不修改）` : '未配置，请输入密钥'"
            />
            <button
              v-if="form[item.key]"
              class="ghost-button"
              type="button"
              @click="resetSecret(item.key)"
            >
              清除输入
            </button>
          </div>

          <el-input
            v-else
            v-model="form[item.key]"
            class="config-input"
            :placeholder="item.default ? `默认值：${item.default}` : '未配置'"
          />
        </div>

        <div class="action-row">
          <button class="primary-button" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存配置' }}
          </button>
          <button class="ghost-button" :disabled="saving" @click="load">重新加载</button>
        </div>
      </template>
    </section>
  </section>
</template>

<style scoped>
.config-item {
  display: grid;
  gap: 8px;
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
}
.config-item:last-of-type {
  border-bottom: none;
}
.config-item-head {
  display: grid;
  gap: 4px;
}
.config-key {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
}
.config-source {
  margin-left: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
}
.config-source--db {
  color: var(--el-color-success, #3a7d5d);
}
.config-desc {
  margin: 0;
  font-size: 12px;
  color: var(--el-text-color-regular, #606266);
}
.config-input {
  max-width: 520px;
}
.config-secret {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.action-row {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 16px;
}
</style>
