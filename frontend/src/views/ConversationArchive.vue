<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '../utils/api'

interface ArchiveMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  turn_index: number
  guidance_stage: string
  created_at: string
}

interface ArchiveConversation {
  id: number
  student_id: number
  student_name: string
  student_username: string
  grade_label?: string | null
  classroom_label?: string | null
  subject: string
  topic: string
  deleted_by_student: boolean
  deleted_by_student_at?: string | null
  created_at: string
  updated_at: string
  messages: ArchiveMessage[]
}

const filters = reactive({
  student_id: '',
  subject: '',
  deleted_by_student: '',
  limit: 200,
})
const rows = ref<ArchiveConversation[]>([])
const loading = ref(false)
const exporting = ref(false)

function formatTime(value?: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
}

function buildParams(includeLimit = true) {
  const params: Record<string, string | number | boolean> = {}
  if (filters.student_id.trim()) {
    params.student_id = Number(filters.student_id.trim())
  }
  if (filters.subject.trim()) {
    params.subject = filters.subject.trim()
  }
  if (filters.deleted_by_student) {
    params.deleted_by_student = filters.deleted_by_student === 'true'
  }
  if (includeLimit) {
    params.limit = filters.limit
  }
  return params
}

async function loadArchive() {
  loading.value = true
  try {
    const { data } = await api.get<ArchiveConversation[]>('/admin/conversation-archive', {
      params: buildParams(),
    })
    rows.value = data
  } catch (error) {
    console.error(error)
    ElMessage.error('会话归档加载失败')
  } finally {
    loading.value = false
  }
}

async function exportArchive() {
  exporting.value = true
  try {
    const { data } = await api.get('/admin/conversation-archive/export', {
      params: buildParams(false),
      responseType: 'blob',
    })
    const url = URL.createObjectURL(data)
    const link = document.createElement('a')
    link.href = url
    link.download = `studyagent-conversation-archive-${Date.now()}.csv`
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    console.error(error)
    ElMessage.error('导出失败')
  } finally {
    exporting.value = false
  }
}

onMounted(loadArchive)
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Conversation Archive</p>
          <h2>会话归档</h2>
        </div>
        <div class="toolbar">
          <button class="ghost-button" :disabled="loading" @click="loadArchive">刷新</button>
          <button class="primary-button" :disabled="exporting" @click="exportArchive">导出 CSV</button>
        </div>
      </div>
      <div class="toolbar">
        <el-input v-model="filters.student_id" clearable placeholder="学生 ID" />
        <el-input v-model="filters.subject" clearable placeholder="学科" />
        <el-select v-model="filters.deleted_by_student" clearable placeholder="清除状态">
          <el-option label="学生已清除" value="true" />
          <el-option label="仍在学生端显示" value="false" />
        </el-select>
        <el-select v-model="filters.limit" placeholder="查看条数">
          <el-option :label="100" :value="100" />
          <el-option :label="200" :value="200" />
          <el-option :label="500" :value="500" />
          <el-option :label="1000" :value="1000" />
        </el-select>
        <button class="primary-button" @click="loadArchive">应用筛选</button>
      </div>
    </section>

    <section class="panel">
      <div class="table-like">
        <article v-for="item in rows" :key="item.id" class="task-card">
          <div class="task-card-head">
            <strong>{{ item.topic || `${item.subject}答疑` }}</strong>
            <span>{{ formatTime(item.updated_at) }}</span>
          </div>
          <div class="detail-chip-group">
            <span class="detail-chip">{{ item.student_name }}（{{ item.student_username }}）</span>
            <span class="detail-chip">{{ item.classroom_label || item.grade_label || '-' }}</span>
            <span class="detail-chip">{{ item.subject }}</span>
            <span class="detail-chip">{{ item.deleted_by_student ? '学生已清除' : '学生端显示中' }}</span>
            <span v-if="item.deleted_by_student_at" class="detail-chip">清除于 {{ formatTime(item.deleted_by_student_at) }}</span>
          </div>
          <div class="table-like">
            <div v-for="message in item.messages" :key="message.id" class="mono-block">
              <strong>{{ message.role }} #{{ message.turn_index }}</strong>
              <span> {{ formatTime(message.created_at) }}</span>
              <p>{{ message.content }}</p>
            </div>
          </div>
        </article>
        <p v-if="!rows.length" class="panel-subcopy">暂无会话记录。</p>
      </div>
    </section>
  </section>
</template>
