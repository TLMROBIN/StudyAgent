<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import MetricTile from '../components/MetricTile.vue'
import { api } from '../utils/api'

interface OverviewData {
  total_questions: number
  resolved_rate: number
  average_turns: number
  by_subject: Array<{ subject: string; count: number }>
}

interface UsageTrendSeries {
  name: string
  subject?: string | null
  data: number[]
}

interface UsageTrend {
  granularity: 'day' | 'week' | 'month'
  start_date: string
  end_date: string
  labels: string[]
  available_subjects: string[]
  series: UsageTrendSeries[]
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
const trendLoading = ref(false)
const trendGranularity = ref<'day' | 'week' | 'month'>('day')
const selectedSubjects = ref<string[]>([])
const today = new Date()
const start = new Date(today)
start.setDate(today.getDate() - 29)
const trendDateRange = ref<[string, string]>([toDateValue(start), toDateValue(today)])
const trend = ref<UsageTrend>({
  granularity: 'day',
  start_date: trendDateRange.value[0],
  end_date: trendDateRange.value[1],
  labels: [],
  available_subjects: [],
  series: [],
})
const trendColors = ['#0f766e', '#db6b2c', '#2563eb', '#7c3aed', '#be123c', '#15803d', '#b45309', '#0891b2', '#4b5563']
const granularityOptions = [
  { label: '每日', value: 'day' },
  { label: '每周', value: 'week' },
  { label: '每月', value: 'month' },
]

function toDateValue(value: Date) {
  const year = value.getFullYear()
  const month = `${value.getMonth() + 1}`.padStart(2, '0')
  const day = `${value.getDate()}`.padStart(2, '0')
  return `${year}-${month}-${day}`
}

const chartModel = computed(() => {
  const labels = trend.value.labels
  const series = trend.value.series
  const width = 760
  const height = 280
  const padding = { top: 24, right: 24, bottom: 42, left: 44 }
  const plotWidth = width - padding.left - padding.right
  const plotHeight = height - padding.top - padding.bottom
  const maxValue = Math.max(1, ...series.flatMap((item) => item.data))
  const xFor = (index: number) => {
    if (labels.length <= 1) {
      return padding.left + plotWidth / 2
    }
    return padding.left + (index / (labels.length - 1)) * plotWidth
  }
  const yFor = (value: number) => padding.top + plotHeight - (value / maxValue) * plotHeight
  const labelStep = Math.max(1, Math.ceil(labels.length / 6))

  return {
    width,
    height,
    maxValue,
    grid: [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
      const value = Math.round(maxValue * (1 - ratio))
      return { y: padding.top + plotHeight * ratio, value }
    }),
    xLabels: labels
      .map((label, index) => ({ label, index, x: xFor(index) }))
      .filter((item, index) => index === 0 || index === labels.length - 1 || index % labelStep === 0),
    lines: series.map((item, seriesIndex) => {
      const points = item.data.map((value, index) => ({
        x: xFor(index),
        y: yFor(value),
        value,
        label: labels[index],
      }))
      return {
        ...item,
        color: trendColors[seriesIndex % trendColors.length],
        points,
        path: points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' '),
      }
    }),
  }
})

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

async function loadTrend() {
  trendLoading.value = true
  try {
    const params = new URLSearchParams()
    params.set('granularity', trendGranularity.value)
    const [startDate, endDate] = trendDateRange.value || []
    if (startDate) {
      params.set('start_date', startDate)
    }
    if (endDate) {
      params.set('end_date', endDate)
    }
    selectedSubjects.value.forEach((subject) => params.append('subjects', subject))
    const { data } = await api.get<UsageTrend>('/stats/usage-trend', { params })
    trend.value = data
  } catch (error) {
    console.error(error)
    ElMessage.error('趋势数据加载失败')
  } finally {
    trendLoading.value = false
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

watch([trendGranularity, selectedSubjects, trendDateRange], loadTrend, { deep: true })

onMounted(() => {
  void loadDashboard()
  void loadTrend()
})
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
      <div class="panel-header panel-header--wrap">
        <div>
          <p class="eyebrow">Usage Trend</p>
          <h2>使用次数变化</h2>
        </div>
        <div class="trend-controls">
          <el-segmented v-model="trendGranularity" :options="granularityOptions" />
          <el-date-picker
            v-model="trendDateRange"
            type="daterange"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            value-format="YYYY-MM-DD"
            :clearable="false"
          />
        </div>
      </div>
      <div class="trend-subjects">
        <span>学科</span>
        <el-checkbox-group v-model="selectedSubjects">
          <el-checkbox v-for="subject in trend.available_subjects" :key="subject" :label="subject" :value="subject" />
        </el-checkbox-group>
      </div>
      <div v-loading="trendLoading" class="trend-chart">
        <svg
          v-if="trend.labels.length"
          class="trend-svg"
          :viewBox="`0 0 ${chartModel.width} ${chartModel.height}`"
          role="img"
          aria-label="使用次数折线图"
        >
          <g class="trend-grid">
            <g v-for="line in chartModel.grid" :key="line.y">
              <line x1="44" x2="736" :y1="line.y" :y2="line.y" />
              <text x="32" :y="line.y + 4" text-anchor="end">{{ line.value }}</text>
            </g>
          </g>
          <g class="trend-axis">
            <text v-for="item in chartModel.xLabels" :key="`${item.label}-${item.index}`" :x="item.x" y="266" text-anchor="middle">
              {{ item.label }}
            </text>
          </g>
          <g v-for="line in chartModel.lines" :key="line.name">
            <path class="trend-line" :d="line.path" :stroke="line.color" />
            <circle
              v-for="point in line.points"
              :key="`${line.name}-${point.label}`"
              class="trend-point"
              :cx="point.x"
              :cy="point.y"
              r="4"
              :fill="line.color"
            >
              <title>{{ line.name }} · {{ point.label }}：{{ point.value }}</title>
            </circle>
          </g>
        </svg>
        <p v-else class="panel-subcopy">当前范围暂无使用数据。</p>
      </div>
      <div class="trend-legend">
        <span v-for="line in chartModel.lines" :key="line.name" class="trend-legend-item">
          <i :style="{ background: line.color }"></i>
          {{ line.name }}
        </span>
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
