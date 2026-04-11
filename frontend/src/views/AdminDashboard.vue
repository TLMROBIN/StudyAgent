<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import MetricTile from '../components/MetricTile.vue'
import { api } from '../utils/api'

interface OverviewData {
  total_questions: number
  resolved_rate: number
  average_turns: number
  by_subject: Array<{ subject: string; count: number }>
}

interface ClassroomStat {
  classroom_label: string
  student_count: number
  total_conversations: number
  resolved_rate: number
  average_turns: number
}

interface StudentPortrait {
  student_id: number
  student_name: string
  login_account?: string | null
  classroom_label?: string | null
  total_conversations: number
  resolved_rate: number
  focus_subject?: string | null
  fallback_ratio: number
  last_active_at?: string | null
}

const overview = ref<OverviewData>({
  total_questions: 0,
  resolved_rate: 0,
  average_turns: 0,
  by_subject: [],
})
const classStats = ref<ClassroomStat[]>([])
const portraits = ref<StudentPortrait[]>([])
const loading = ref(false)

function formatPercent(value: number) {
  return `${(value * 100).toFixed(1)}%`
}

function formatTime(value?: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

async function loadDashboard() {
  loading.value = true
  try {
    const [{ data: overviewData }, { data: classData }, { data: portraitData }] = await Promise.all([
      api.get<OverviewData>('/stats/overview'),
      api.get<ClassroomStat[]>('/stats/classes'),
      api.get<StudentPortrait[]>('/stats/portraits', { params: { limit: 12 } }),
    ])
    overview.value = overviewData
    classStats.value = classData
    portraits.value = portraitData
  } catch (error) {
    console.error(error)
    ElMessage.error('统计数据加载失败')
  } finally {
    loading.value = false
  }
}

async function exportStats() {
  try {
    const { data } = await api.get('/stats/export', {
      params: { format: 'xlsx' },
      responseType: 'blob',
    })
    const url = URL.createObjectURL(data)
    const link = document.createElement('a')
    link.href = url
    link.download = `studyagent-stats-${Date.now()}.xlsx`
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    console.error(error)
    ElMessage.error('导出失败')
  }
}

onMounted(loadDashboard)
</script>

<template>
  <section class="dashboard-stack">
    <div class="panel-header">
      <div>
        <p class="eyebrow">Admin Metrics</p>
        <h2>全校答疑概览</h2>
      </div>
      <div class="toolbar">
        <button class="ghost-button" :disabled="loading" @click="loadDashboard">刷新</button>
        <button class="primary-button" @click="exportStats">导出统计</button>
      </div>
    </div>
    <div class="metric-grid">
      <MetricTile label="累计提问" :value="overview.total_questions" hint="学生所有会话累计" />
      <MetricTile label="已解决率" :value="`${(overview.resolved_rate * 100).toFixed(1)}%`" hint="学生主动标记完成" />
      <MetricTile label="平均轮次" :value="overview.average_turns" hint="反映引导深度" />
    </div>
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Subject Mix</p>
          <h2>学科分布</h2>
        </div>
      </div>
      <div class="subject-bars">
        <article v-for="item in overview.by_subject" :key="item.subject" class="subject-row">
          <span>{{ item.subject }}</span>
          <div class="subject-bar-track">
            <div class="subject-bar-fill" :style="{ width: `${Math.max(item.count * 12, 8)}px` }"></div>
          </div>
          <strong>{{ item.count }}</strong>
        </article>
      </div>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Classroom View</p>
          <h2>班级统计</h2>
        </div>
      </div>
      <div class="table-like">
        <article v-for="item in classStats" :key="item.classroom_label" class="table-row table-row-wrap">
          <div class="table-main">
            <strong>{{ item.classroom_label }}</strong>
            <span>{{ item.student_count }} 名学生</span>
          </div>
          <div class="detail-chip-group">
            <span class="detail-chip">会话 {{ item.total_conversations }}</span>
            <span class="detail-chip">解决率 {{ formatPercent(item.resolved_rate) }}</span>
            <span class="detail-chip">平均轮次 {{ item.average_turns }}</span>
          </div>
        </article>
        <p v-if="!classStats.length" class="panel-subcopy">暂无班级统计数据。</p>
      </div>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Student Portraits</p>
          <h2>学生画像</h2>
        </div>
      </div>
      <div class="table-like">
        <article v-for="item in portraits" :key="item.student_id" class="table-row table-row-wrap">
          <div class="table-main">
            <strong>{{ item.student_name }}</strong>
            <span>{{ item.classroom_label || '未分班' }} · {{ item.login_account || '-' }}</span>
          </div>
          <div class="detail-chip-group">
            <span class="detail-chip">会话 {{ item.total_conversations }}</span>
            <span class="detail-chip">解决率 {{ formatPercent(item.resolved_rate) }}</span>
            <span class="detail-chip">关注学科 {{ item.focus_subject || '-' }}</span>
            <span class="detail-chip">兜底占比 {{ formatPercent(item.fallback_ratio) }}</span>
            <span class="detail-chip">最近活跃 {{ formatTime(item.last_active_at) }}</span>
          </div>
        </article>
        <p v-if="!portraits.length" class="panel-subcopy">暂无学生画像数据。</p>
      </div>
    </section>
  </section>
</template>
