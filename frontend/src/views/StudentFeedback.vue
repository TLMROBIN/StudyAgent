<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import {
  createFeedback,
  fetchFeedbackUnreadSummary,
  fetchMyFeedback,
  fetchReleaseNotes,
  markFeedbackRead,
  markReleaseNoteRead,
  type FeedbackUnreadSummary,
  type ReleaseNoteItem,
  type StudentFeedbackItem,
} from '../utils/api'

type FeedbackSection = 'feedback' | 'release-notes'

const loading = ref(false)
const releaseNotesLoading = ref(false)
const saving = ref(false)
const content = ref('')
const activeSection = ref<FeedbackSection>('feedback')
const items = ref<StudentFeedbackItem[]>([])
const releaseNotes = ref<ReleaseNoteItem[]>([])
const dailyLimit = ref(2)
const dailyRemaining = ref(0)
const feedbackBanned = ref(false)
const unreadSummary = ref<FeedbackUnreadSummary>({
  unread_feedback_replies: 0,
  unread_release_notes: 0,
  has_unread: false,
})

const hasUnreadFeedbackReplies = computed(() => unreadSummary.value.unread_feedback_replies > 0)
const hasUnreadReleaseNotes = computed(() => unreadSummary.value.unread_release_notes > 0)

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
    unreadSummary.value.unread_feedback_replies = data.unread_reply_count
    unreadSummary.value.has_unread = unreadSummary.value.unread_feedback_replies + unreadSummary.value.unread_release_notes > 0
  } catch (error) {
    console.error(error)
    ElMessage.error('反馈记录加载失败')
  } finally {
    loading.value = false
  }
}

async function loadUnreadSummary() {
  try {
    unreadSummary.value = await fetchFeedbackUnreadSummary()
  } catch (error) {
    console.error(error)
  }
}

async function loadReleaseNotes() {
  releaseNotesLoading.value = true
  try {
    const data = await fetchReleaseNotes()
    releaseNotes.value = data.items
    unreadSummary.value.unread_release_notes = data.unread_count
    unreadSummary.value.has_unread = unreadSummary.value.unread_feedback_replies + unreadSummary.value.unread_release_notes > 0
  } catch (error) {
    console.error(error)
    ElMessage.error('更新日志加载失败')
  } finally {
    releaseNotesLoading.value = false
  }
}

async function switchSection(section: FeedbackSection) {
  activeSection.value = section
  if (section === 'release-notes') {
    await loadReleaseNotes()
  } else {
    await loadFeedback()
  }
}

async function markFeedbackItemRead(item: StudentFeedbackItem) {
  if (!item.student_unread) {
    return
  }
  try {
    const updated = await markFeedbackRead(item.id)
    items.value = items.value.map((current) => (current.id === updated.id ? updated : current))
    await loadUnreadSummary()
  } catch (error) {
    console.error(error)
  }
}

async function markReleaseNoteItemRead(item: ReleaseNoteItem) {
  if (item.is_read) {
    return
  }
  try {
    const updated = await markReleaseNoteRead(item.id)
    releaseNotes.value = releaseNotes.value.map((current) => (current.id === updated.id ? updated : current))
    await loadUnreadSummary()
  } catch (error) {
    console.error(error)
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
  void loadReleaseNotes()
  void loadUnreadSummary()
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
        <div class="row-actions">
          <button
            :class="activeSection === 'feedback' ? 'primary-button' : 'ghost-button'"
            @click="switchSection('feedback')"
          >
            意见反馈
            <span v-if="hasUnreadFeedbackReplies" class="unread-dot unread-feedback-dot"></span>
          </button>
          <button
            :class="activeSection === 'release-notes' ? 'primary-button' : 'ghost-button'"
            @click="switchSection('release-notes')"
          >
            更新日志
            <span v-if="hasUnreadReleaseNotes" class="unread-dot unread-release-dot"></span>
          </button>
          <button class="ghost-button" :disabled="loading || releaseNotesLoading" @click="activeSection === 'feedback' ? loadFeedback() : loadReleaseNotes()">刷新</button>
        </div>
      </div>
      <div v-if="activeSection === 'feedback'" class="detail-chip-group feedback-quota">
        <span :class="['detail-chip', { 'detail-chip--muted': feedbackBanned }]">
          {{ feedbackBanned ? '反馈权限已暂停' : `今日剩余 ${dailyRemaining} / ${dailyLimit} 次` }}
        </span>
      </div>
      <form v-if="activeSection === 'feedback'" class="feedback-form" @submit.prevent="submitFeedback">
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

    <section v-if="activeSection === 'feedback'" class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">History</p>
          <h2>我的反馈记录</h2>
        </div>
      </div>
      <div v-loading="loading" class="table-like feedback-list">
        <article v-for="item in items" :key="item.id" class="table-row table-row-wrap feedback-row" @click="markFeedbackItemRead(item)">
          <div class="table-main table-main--grow">
            <strong>
              <span v-if="item.student_unread" class="unread-dot unread-feedback-dot"></span>
              {{ item.content }}
            </strong>
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

    <section v-else class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Release Notes</p>
          <h2>更新日志</h2>
          <p class="panel-subcopy">查看管理员发布的功能更新、使用说明和重要调整。</p>
        </div>
      </div>
      <div v-loading="releaseNotesLoading" class="table-like feedback-list">
        <article
          v-for="item in releaseNotes"
          :key="item.id"
          class="table-row table-row-wrap feedback-row release-note-row"
          @click="markReleaseNoteItemRead(item)"
        >
          <div class="table-main table-main--grow">
            <strong>
              <span v-if="!item.is_read" class="unread-dot unread-release-dot"></span>
              {{ item.title }}
            </strong>
            <span>发布 {{ formatTime(item.published_at || item.created_at) }}</span>
          </div>
          <p class="release-note-content">{{ item.content }}</p>
        </article>
        <p v-if="!releaseNotes.length" class="panel-subcopy">暂无更新日志。</p>
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

.unread-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 6px;
  border-radius: 999px;
  background: #dc2626;
  box-shadow: 0 0 0 3px rgba(220, 38, 38, 0.12);
  vertical-align: middle;
}

.release-note-row {
  cursor: pointer;
}

.release-note-content {
  margin: 0;
  white-space: pre-wrap;
}
</style>
