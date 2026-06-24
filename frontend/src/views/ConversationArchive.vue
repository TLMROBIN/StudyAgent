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
  llm_model_key?: string | null
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

interface ArchiveConversationList {
  items: ArchiveConversation[]
  total: number
  page: number
  page_size: number
}

const filters = reactive({
  student_id: '',
  student_name: '',
  subject: '',
  deleted_by_student: '',
})
const rows = ref<ArchiveConversation[]>([])
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const loading = ref(false)
const exporting = ref(false)

function formatTime(value?: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
}

function buildParams(includePagination = true) {
  const params: Record<string, string | number | boolean> = {}
  if (filters.student_id.trim()) {
    params.student_id = Number(filters.student_id.trim())
  }
  if (filters.student_name.trim()) {
    params.student_name = filters.student_name.trim()
  }
  if (filters.subject.trim()) {
    params.subject = filters.subject.trim()
  }
  if (filters.deleted_by_student) {
    params.deleted_by_student = filters.deleted_by_student === 'true'
  }
  if (includePagination) {
    params.page = page.value
    params.page_size = pageSize.value
  }
  return params
}

async function loadArchive() {
  loading.value = true
  try {
    const { data } = await api.get<ArchiveConversationList>('/admin/conversation-archive', {
      params: buildParams(),
    })
    rows.value = data.items
    total.value = data.total
    page.value = data.page
    pageSize.value = data.page_size
  } catch (error) {
    console.error(error)
    ElMessage.error('会话归档加载失败')
  } finally {
    loading.value = false
  }
}

function applyFilters() {
  page.value = 1
  loadArchive()
}

function handlePageChange(nextPage: number) {
  page.value = nextPage
  loadArchive()
}

function handlePageSizeChange(nextPageSize: number) {
  pageSize.value = nextPageSize
  page.value = 1
  loadArchive()
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
      <div class="toolbar toolbar-wrap">
        <el-input v-model="filters.student_name" class="toolbar-field" clearable placeholder="学生姓名" @keyup.enter="applyFilters" />
        <el-input v-model="filters.student_id" class="toolbar-field" clearable placeholder="学生 ID" @keyup.enter="applyFilters" />
        <el-input v-model="filters.subject" class="toolbar-field" clearable placeholder="学科" @keyup.enter="applyFilters" />
        <el-select v-model="filters.deleted_by_student" class="toolbar-field" clearable placeholder="清除状态">
          <el-option label="学生已清除" value="true" />
          <el-option label="仍在学生端显示" value="false" />
        </el-select>
        <button class="primary-button" @click="applyFilters">应用筛选</button>
      </div>
    </section>

    <section class="panel">
      <div class="archive-pagination">
        <p class="panel-subcopy">共 {{ total }} 条归档会话</p>
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          class="table-pagination"
          :disabled="loading"
          :page-sizes="[10, 20, 50, 100]"
          :total="total"
          layout="sizes, prev, pager, next, jumper"
          @current-change="handlePageChange"
          @size-change="handlePageSizeChange"
        />
      </div>
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
              <span v-if="message.llm_model_key"> 模型 {{ message.llm_model_key }}</span>
              <p>{{ message.content }}</p>
            </div>
          </div>
        </article>
        <p v-if="!rows.length" class="panel-subcopy">暂无会话记录。</p>
      </div>
    </section>
  </section>
</template>

<style scoped>
.archive-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.table-pagination {
  margin-left: auto;
}
</style>
