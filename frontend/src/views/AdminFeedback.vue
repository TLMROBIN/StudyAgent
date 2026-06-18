<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  banStudentFeedback,
  fetchAdminFeedback,
  replyAdminFeedback,
  type AdminFeedbackItem,
  unbanStudentFeedback,
} from '../utils/api'

const loading = ref(false)
const savingId = ref<number | null>(null)
const items = ref<AdminFeedbackItem[]>([])
const replyDrafts = reactive<Record<number, string>>({})

const unrepliedCount = computed(() => items.value.filter((item) => !item.reply_content).length)

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
    items.value = await fetchAdminFeedback()
    for (const item of items.value) {
      replyDrafts[item.id] = item.reply_content || ''
    }
  } catch (error) {
    console.error(error)
    ElMessage.error('反馈列表加载失败')
  } finally {
    loading.value = false
  }
}

async function saveReply(item: AdminFeedbackItem) {
  const reply = (replyDrafts[item.id] || '').trim()
  if (!reply) {
    ElMessage.info('请填写回复内容')
    return
  }
  savingId.value = item.id
  try {
    await replyAdminFeedback(item.id, { reply_content: reply })
    ElMessage.success('回复已保存')
    await loadFeedback()
  } catch (error) {
    console.error(error)
    ElMessage.error(responseDetail(error, '回复保存失败'))
  } finally {
    savingId.value = null
  }
}

async function banFeedback(item: AdminFeedbackItem) {
  const reason = await ElMessageBox.prompt(
    `暂停 ${item.student_name} 的反馈权限，可填写原因。`,
    '暂停反馈权限',
    {
      confirmButtonText: '暂停',
      cancelButtonText: '取消',
      inputPlaceholder: '原因（可选）',
    },
  ).then((result) => result.value).catch(() => null)
  if (reason === null) {
    return
  }
  savingId.value = item.id
  try {
    await banStudentFeedback(item.student_id, { reason: reason.trim() || null })
    ElMessage.success('已暂停该学生的反馈权限')
    await loadFeedback()
  } catch (error) {
    console.error(error)
    ElMessage.error(responseDetail(error, '反馈权限暂停失败'))
  } finally {
    savingId.value = null
  }
}

async function unbanFeedback(item: AdminFeedbackItem) {
  savingId.value = item.id
  try {
    await unbanStudentFeedback(item.student_id)
    ElMessage.success('已恢复该学生的反馈权限')
    await loadFeedback()
  } catch (error) {
    console.error(error)
    ElMessage.error(responseDetail(error, '反馈权限恢复失败'))
  } finally {
    savingId.value = null
  }
}

onMounted(() => {
  void loadFeedback()
})
</script>

<template>
  <section class="dashboard-stack">
    <div class="panel-header">
      <div>
        <p class="eyebrow">Student Feedback</p>
        <h2>意见反馈管理</h2>
        <p class="panel-subcopy">查看学生提交的意见，回复后学生可在自己的反馈页看到处理结果。</p>
      </div>
      <div class="toolbar">
        <span class="detail-chip">待回复 {{ unrepliedCount }}</span>
        <button class="ghost-button" :disabled="loading" @click="loadFeedback">刷新</button>
      </div>
    </div>

    <section v-loading="loading" class="panel feedback-admin-list">
      <article v-for="item in items" :key="item.id" class="feedback-admin-card">
        <div class="feedback-admin-head">
          <div>
            <strong>{{ item.student_name }}</strong>
            <span>{{ item.classroom_label || item.grade_label || '未分班' }} · {{ item.student_username }}</span>
          </div>
          <div class="detail-chip-group">
            <span :class="['detail-chip', { 'detail-chip--muted': item.reply_content }]">
              {{ item.reply_content ? '已回复' : '待回复' }}
            </span>
            <span :class="['detail-chip', { 'detail-chip--muted': item.student_feedback_banned }]">
              {{ item.student_feedback_banned ? '反馈已暂停' : '可反馈' }}
            </span>
          </div>
        </div>
        <div class="feedback-admin-content">
          <p>{{ item.content }}</p>
          <span>提交 {{ formatTime(item.created_at) }}</span>
        </div>
        <div class="feedback-admin-reply">
          <textarea
            v-model="replyDrafts[item.id]"
            class="textarea"
            maxlength="1000"
            rows="3"
            placeholder="给学生的回复"
            :disabled="savingId === item.id"
          ></textarea>
          <div class="row-actions">
            <span v-if="item.reply_content" class="panel-subcopy">
              上次回复：{{ item.replied_by_name || '管理员' }} · {{ formatTime(item.replied_at) }}
            </span>
            <button class="primary-button" :disabled="savingId === item.id" @click="saveReply(item)">
              保存回复
            </button>
            <button
              v-if="!item.student_feedback_banned"
              class="ghost-button ghost-button--danger"
              :disabled="savingId === item.id"
              @click="banFeedback(item)"
            >
              暂停反馈
            </button>
            <button
              v-else
              class="ghost-button"
              :disabled="savingId === item.id"
              @click="unbanFeedback(item)"
            >
              恢复反馈
            </button>
          </div>
        </div>
      </article>
      <p v-if="!items.length" class="panel-subcopy">暂无学生反馈。</p>
    </section>
  </section>
</template>

<style scoped>
.feedback-admin-list {
  display: grid;
  gap: 14px;
}

.feedback-admin-card {
  display: grid;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(255, 252, 246, 0.72);
}

.feedback-admin-head,
.feedback-admin-content,
.feedback-admin-reply {
  display: grid;
  gap: 10px;
}

.feedback-admin-head {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
}

.feedback-admin-head span,
.feedback-admin-content span {
  color: var(--muted);
}

.feedback-admin-content p {
  margin: 0;
  white-space: pre-wrap;
}

@media (max-width: 780px) {
  .feedback-admin-head {
    grid-template-columns: 1fr;
  }
}
</style>
