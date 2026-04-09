<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '../utils/api'

interface AuditLogRow {
  id: number
  actor_id?: number | null
  actor_name?: string | null
  action: string
  target_type: string
  target_id?: string | null
  result: string
  ip_address?: string | null
  detail: Record<string, unknown>
  created_at: string
}

const filters = reactive({
  action: '',
  result: '',
  limit: 100,
})
const logs = ref<AuditLogRow[]>([])
const loading = ref(false)

const actionOptions = computed(() => Array.from(new Set(logs.value.map((item) => item.action))).sort())

async function loadLogs() {
  loading.value = true
  try {
    const params: Record<string, string | number> = { limit: filters.limit }
    if (filters.action) {
      params.action = filters.action
    }
    if (filters.result) {
      params.result = filters.result
    }
    const { data } = await api.get<AuditLogRow[]>('/admin/audit-logs', { params })
    logs.value = data
  } catch (error) {
    console.error(error)
    ElMessage.error('审计日志加载失败')
  } finally {
    loading.value = false
  }
}

function formatDetail(detail: Record<string, unknown>) {
  return JSON.stringify(detail, null, 2)
}

function formatTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
}

onMounted(loadLogs)
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Audit Trail</p>
          <h2>操作审计日志</h2>
        </div>
        <button class="ghost-button" :disabled="loading" @click="loadLogs">刷新</button>
      </div>
      <div class="toolbar">
        <el-select v-model="filters.action" clearable placeholder="按动作筛选">
          <el-option v-for="item in actionOptions" :key="item" :label="item" :value="item" />
        </el-select>
        <el-select v-model="filters.result" clearable placeholder="按结果筛选">
          <el-option label="success" value="success" />
          <el-option label="accepted" value="accepted" />
        </el-select>
        <el-select v-model="filters.limit" placeholder="查看条数">
          <el-option :label="50" :value="50" />
          <el-option :label="100" :value="100" />
          <el-option :label="200" :value="200" />
        </el-select>
        <button class="primary-button" @click="loadLogs">应用筛选</button>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Recent Events</p>
          <h2>最近操作</h2>
        </div>
      </div>
      <div class="table-like">
        <article v-for="item in logs" :key="item.id" class="task-card">
          <div class="task-card-head">
            <strong>{{ item.action }}</strong>
            <span>{{ formatTime(item.created_at) }}</span>
          </div>
          <div class="detail-chip-group">
            <span class="detail-chip">操作者 {{ item.actor_name || item.actor_id || '系统' }}</span>
            <span class="detail-chip">目标 {{ item.target_type }}#{{ item.target_id || '-' }}</span>
            <span class="detail-chip">结果 {{ item.result }}</span>
            <span class="detail-chip">IP {{ item.ip_address || '-' }}</span>
          </div>
          <pre class="mono-block">{{ formatDetail(item.detail) }}</pre>
        </article>
        <p v-if="!logs.length" class="panel-subcopy">暂无审计日志。</p>
      </div>
    </section>
  </section>
</template>
