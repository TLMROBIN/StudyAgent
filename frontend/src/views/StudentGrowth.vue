<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import {
  fetchIncentivePraises,
  fetchIncentiveReport,
  fetchIncentiveSummary,
  markIncentivePraisesRead,
  type IncentivePraise,
  type IncentiveReport,
  type IncentiveSummary,
} from '../utils/api'

const LEVEL_TITLES = ['好奇新芽', '提问学徒', '追问行者', '思路编织者', '推导能手', '独立解题人', '举一反三者', '思辨达人', '融会贯通者', '思维大师']
const period = ref<'week' | 'month'>('week')
const loading = ref(false)
const summary = ref<IncentiveSummary>({
  total_points: 0,
  level: 1,
  next_level_points: null,
  current_streak_days: 0,
  longest_streak_days: 0,
  badges: [],
  counters: {},
  has_unread_praise: false,
})
const report = ref<IncentiveReport | null>(null)
const praises = ref<IncentivePraise[]>([])

const levelTitle = computed(() => LEVEL_TITLES[Math.max(0, summary.value.level - 1)] || '思维成长者')
const progress = computed(() => {
  if (!summary.value.next_level_points) return 100
  const currentThreshold = [0, 50, 120, 250, 450, 700, 1000, 1400, 1900, 2500][
    Math.max(0, summary.value.level - 1)
  ] || 0
  return Math.min(
    100,
    Math.round(
      ((summary.value.total_points - currentThreshold) / (summary.value.next_level_points - currentThreshold)) * 100,
    ),
  )
})
const maxDailyPoints = computed(() => Math.max(1, ...(report.value?.daily_points.map((item) => item.points) || [1])))

async function load() {
  loading.value = true
  try {
    const [summaryData, reportData, praiseData] = await Promise.all([
      fetchIncentiveSummary(),
      fetchIncentiveReport(period.value),
      fetchIncentivePraises(),
    ])
    summary.value = summaryData
    report.value = reportData
    praises.value = praiseData
    if (summaryData.has_unread_praise) {
      summary.value = await markIncentivePraisesRead()
    }
  } catch (error) {
    console.error(error)
    ElMessage.error('成长记录加载失败')
  } finally {
    loading.value = false
  }
}

async function changePeriod(value: 'week' | 'month') {
  period.value = value
  await load()
}

function printReport() {
  window.print()
}

onMounted(load)
</script>

<template>
  <section v-loading="loading" class="dashboard-stack growth-page">
    <section class="panel growth-hero">
      <div>
        <p class="eyebrow">Personal Growth</p>
        <h2>{{ levelTitle }} · L{{ summary.level }}</h2>
        <p class="panel-subcopy">只和昨天的自己比较。每一次认真回应、推导和反思，都会留下成长记录。</p>
      </div>
      <div class="growth-score">
        <strong>{{ summary.total_points }}</strong>
        <span>成长积分</span>
      </div>
      <el-progress :percentage="progress" :stroke-width="12" />
      <div class="detail-chip-group">
        <span class="detail-chip">连续学习 {{ summary.current_streak_days }} 天</span>
        <span class="detail-chip">最长 {{ summary.longest_streak_days }} 天</span>
        <span class="detail-chip">下一等级 {{ summary.next_level_points ?? '已达最高级' }}</span>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div><p class="eyebrow">Badges</p><h2>徽章墙</h2></div>
      </div>
      <div v-if="summary.badges.length" class="badge-wall">
        <article v-for="badge in summary.badges" :key="badge" class="badge-card"><span>✦</span><strong>{{ badge }}</strong></article>
      </div>
      <p v-else class="panel-subcopy">继续跟随引导完成推导，第一枚徽章正在靠近。</p>
    </section>

    <section class="panel">
      <div class="panel-header panel-header--wrap">
        <div><p class="eyebrow">Growth Report</p><h2>成长报告</h2></div>
        <div class="growth-report-actions">
          <el-button plain @click="printReport">打印报告</el-button>
          <el-segmented :model-value="period" :options="[{ label: '本周', value: 'week' }, { label: '本月', value: 'month' }]" @change="changePeriod" />
        </div>
      </div>
      <p class="growth-narrative">{{ report?.narrative || '本期还没有足够的学习记录。' }}</p>
      <div class="metric-grid growth-metrics">
        <article class="metric-tile"><span>引导跟随率</span><strong>{{ ((report?.followup_rate || 0) * 100).toFixed(0) }}%</strong></article>
        <article class="metric-tile"><span>非兜底解决率</span><strong>{{ ((report?.early_resolve_rate || 0) * 100).toFixed(0) }}%</strong></article>
        <article class="metric-tile"><span>本期新增积分</span><strong>{{ report?.daily_points.reduce((sum, item) => sum + item.points, 0) || 0 }}</strong></article>
      </div>
      <div class="growth-chart" aria-label="每日成长积分">
        <div v-for="item in report?.daily_points || []" :key="item.date" class="growth-bar-item">
          <span class="growth-bar" :style="{ height: `${Math.max(8, (item.points / maxDailyPoints) * 120)}px` }"></span>
          <strong>{{ item.points }}</strong><small>{{ item.date.slice(5) }}</small>
        </div>
        <p v-if="!report?.daily_points.length" class="panel-subcopy">本期暂无积分曲线。</p>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header"><div><p class="eyebrow">Teacher Praise</p><h2>老师的表扬</h2></div></div>
      <div class="table-like">
        <article v-for="item in praises" :key="item.id" class="table-row table-row-wrap">
          <div class="table-main"><strong>{{ item.teacher_name }}</strong><span>{{ item.content }}</span></div>
          <span class="detail-chip">+{{ item.points }} · {{ new Date(item.created_at).toLocaleDateString() }}</span>
        </article>
        <p v-if="!praises.length" class="panel-subcopy">暂无表扬记录。认真完成思考过程，本身就是值得肯定的成长。</p>
      </div>
    </section>
  </section>
</template>

<style scoped>
.growth-page { max-width: 1120px; margin: 0 auto; }
.growth-hero { display: grid; gap: 18px; }
.growth-score { display: flex; align-items: baseline; gap: 10px; }
.growth-score strong { font-size: clamp(2.6rem, 7vw, 5rem); line-height: 1; color: var(--accent); }
.badge-wall { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.badge-card { display: flex; align-items: center; gap: 10px; padding: 16px; border: 1px solid var(--line); border-radius: 16px; background: rgba(255,255,255,.62); }
.badge-card span { color: #c7831f; font-size: 1.35rem; }
.growth-narrative { padding: 16px 18px; border-radius: 14px; background: rgba(53, 112, 99, .08); line-height: 1.75; }
.growth-metrics { margin: 18px 0; }
.growth-chart { min-height: 170px; display: flex; align-items: flex-end; gap: 12px; overflow-x: auto; padding: 18px 6px 0; }
.growth-report-actions { display: flex; align-items: center; gap: 10px; }
.growth-bar-item { min-width: 42px; display: grid; justify-items: center; gap: 5px; }
.growth-bar { width: 24px; border-radius: 8px 8px 3px 3px; background: linear-gradient(180deg, #70ad97, #326d60); }
@media (max-width: 700px) { .growth-chart { gap: 8px; } }
@media print {
  .growth-report-actions, :global(.app-sidebar), :global(.app-header) { display: none !important; }
  .growth-page { max-width: none; }
  .panel { break-inside: avoid; box-shadow: none; }
}
</style>
