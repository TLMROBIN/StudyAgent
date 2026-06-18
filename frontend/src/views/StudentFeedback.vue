<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import {
  createFeedback,
  fetchMyFeedback,
  type StudentFeedbackItem,
} from '../utils/api'

const loading = ref(false)
const saving = ref(false)
const content = ref('')
const items = ref<StudentFeedbackItem[]>([])
const dailyLimit = ref(2)
const dailyRemaining = ref(0)
const feedbackBanned = ref(false)

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

function responseDetail(error: unknown, fallback: string) {
  const detail = (error as { response?: { data?: { detail?: string } } }).response?.data?.detail
  return detail || fallback
}

async function loadFeedback() {
  loading.value = true
  try {
    const data = await fetchMyFeedback()
    items.value = data.items
    dailyLimit.value = data.daily_limit
    dailyRemaining.value = data.daily_remaining
    feedbackBanned.value = data.feedback_banned
  } catch (error) {
    console.error(error)
    ElMessage.error('反馈记录加载失败')
  } finally {
    loading.value = false
  }
}

async function submitFeedback() {
  const text = content.value.trim()
  if (!text) {
    ElMessage.info('请先填写反馈内容')
    return
  }
  saving.value = true
  try {
    await createFeedback({ content: text })
    content.value = ''
    ElMessage.success('反馈已提交')
    await loadFeedback()
  } catch (error) {
    console.error(error)
    ElMessage.error(responseDetail(error, '反馈提交失败'))
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void loadFeedback()
})
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header panel-header--wrap">
        <div>
          <p class="eyebrow">Feedback</p>
          <h2>意见反馈</h2>
          <p class="panel-subcopy">把使用中遇到的问题或建议发给管理员；管理员回复后会显示在这里。</p>
        </div>
        <button class="ghost-button" :disabled="loading" @click="loadFeedback">刷新</button>
      </div>
      <div class="detail-chip-group feedback-quota">
        <span :class="['detail-chip', { 'detail-chip--muted': feedbackBanned }]">
          {{ feedbackBanned ? '反馈权限已暂停' : `今日剩余 ${dailyRemaining} / ${dailyLimit} 次` }}
        </span>
      </div>
      <form class="feedback-form" @submit.prevent="submitFeedback">
        <textarea
          v-model="content"
          class="textarea"
          maxlength="1000"
          rows="6"
          :disabled="saving || feedbackBanned || dailyRemaining <= 0"
          placeholder="请描述你遇到的问题、建议或希望改进的地方"
        ></textarea>
        <div class="row-actions">
          <span class="panel-subcopy">{{ content.trim().length }}/1000</span>
          <button
            class="primary-button"
            type="submit"
            :disabled="saving || feedbackBanned || dailyRemaining <= 0"
          >
            提交反馈
          </button>
        </div>
      </form>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">History</p>
          <h2>我的反馈记录</h2>
        </div>
      </div>
      <div v-loading="loading" class="table-like feedback-list">
        <article v-for="item in items" :key="item.id" class="table-row table-row-wrap feedback-row">
          <div class="table-main table-main--grow">
            <strong>{{ item.content }}</strong>
            <span>提交 {{ formatTime(item.created_at) }}</span>
          </div>
          <div v-if="item.reply_content" class="feedback-reply">
            <strong>管理员回复</strong>
            <p>{{ item.reply_content }}</p>
            <span>{{ item.replied_by_name || '管理员' }} · {{ formatTime(item.replied_at) }}</span>
          </div>
          <span v-else class="detail-chip detail-chip--muted">等待回复</span>
        </article>
        <p v-if="!items.length" class="panel-subcopy">暂无反馈记录。</p>
      </div>
    </section>
  </section>
</template>

<style scoped>
.feedback-form {
  display: grid;
  gap: 14px;
  margin-top: 18px;
}

.feedback-quota {
  margin-top: 12px;
}

.feedback-list {
  display: grid;
  gap: 12px;
}

.feedback-row {
  align-items: start;
}

.feedback-reply {
  display: grid;
  gap: 6px;
  min-width: min(360px, 100%);
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(15, 118, 110, 0.08);
}

.feedback-reply p {
  margin: 0;
  white-space: pre-wrap;
}

.feedback-reply span {
  color: var(--muted);
}
</style>
