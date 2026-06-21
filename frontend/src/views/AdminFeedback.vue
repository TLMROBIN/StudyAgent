<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  banStudentFeedback,
  createAdminReleaseNote,
  fetchAdminFeedback,
  fetchAdminReleaseNotes,
  replyAdminFeedback,
  type AdminFeedbackItem,
  type ReleaseNoteItem,
  unbanStudentFeedback,
  updateAdminReleaseNote,
} from '../utils/api'

const loading = ref(false)
const releaseNotesLoading = ref(false)
const savingId = ref<number | null>(null)
const releaseNoteSaving = ref(false)
const items = ref<AdminFeedbackItem[]>([])
const releaseNotes = ref<ReleaseNoteItem[]>([])
const replyDrafts = reactive<Record<number, string>>({})
const releaseNoteForm = reactive({
  id: null as number | null,
  title: '',
  content: '',
  is_published: true,
})

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

async function loadReleaseNotes() {
  releaseNotesLoading.value = true
  try {
    releaseNotes.value = await fetchAdminReleaseNotes()
  } catch (error) {
    console.error(error)
    ElMessage.error('更新日志加载失败')
  } finally {
    releaseNotesLoading.value = false
  }
}

function resetReleaseNoteForm() {
  releaseNoteForm.id = null
  releaseNoteForm.title = ''
  releaseNoteForm.content = ''
  releaseNoteForm.is_published = true
}

function editReleaseNote(item: ReleaseNoteItem) {
  releaseNoteForm.id = item.id
  releaseNoteForm.title = item.title
  releaseNoteForm.content = item.content
  releaseNoteForm.is_published = item.is_published
}

async function submitReleaseNote() {
  const title = releaseNoteForm.title.trim()
  const content = releaseNoteForm.content.trim()
  if (!title || !content) {
    ElMessage.info('请填写更新日志标题和内容')
    return
  }
  releaseNoteSaving.value = true
  try {
    if (releaseNoteForm.id) {
      await updateAdminReleaseNote(releaseNoteForm.id, {
        title,
        content,
        is_published: releaseNoteForm.is_published,
      })
      ElMessage.success('更新日志已保存')
    } else {
      await createAdminReleaseNote({
        title,
        content,
        is_published: releaseNoteForm.is_published,
      })
      ElMessage.success(releaseNoteForm.is_published ? '更新日志已发布' : '更新日志草稿已保存')
    }
    resetReleaseNoteForm()
    await loadReleaseNotes()
  } catch (error) {
    console.error(error)
    ElMessage.error(responseDetail(error, '更新日志保存失败'))
  } finally {
    releaseNoteSaving.value = false
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
  void loadReleaseNotes()
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

    <section class="panel release-note-admin-panel">
      <div class="panel-header panel-header--wrap">
        <div>
          <p class="eyebrow">Release Notes</p>
          <h2>发布更新日志</h2>
          <p class="panel-subcopy">发布后，学生端“意见反馈”里的“更新日志”入口会显示红点提醒。</p>
        </div>
        <button v-if="releaseNoteForm.id" class="ghost-button" :disabled="releaseNoteSaving" @click="resetReleaseNoteForm">
          取消编辑
        </button>
      </div>
      <form class="feedback-admin-reply release-note-form" @submit.prevent="submitReleaseNote">
        <el-input
          v-model="releaseNoteForm.title"
          maxlength="120"
          placeholder="更新日志标题"
          :disabled="releaseNoteSaving"
        />
        <textarea
          v-model="releaseNoteForm.content"
          class="textarea"
          maxlength="5000"
          rows="6"
          placeholder="面向学生的更新说明"
          :disabled="releaseNoteSaving"
        ></textarea>
        <label class="release-note-publish-toggle">
          <input v-model="releaseNoteForm.is_published" type="checkbox" />
          立即发布给学生
        </label>
        <div class="row-actions">
          <span class="panel-subcopy">{{ releaseNoteForm.content.trim().length }}/5000</span>
          <button class="primary-button" type="submit" :disabled="releaseNoteSaving">
            {{ releaseNoteForm.id ? '保存更新日志' : '发布更新日志' }}
          </button>
        </div>
      </form>
    </section>

    <section v-loading="releaseNotesLoading" class="panel feedback-admin-list">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Release History</p>
          <h2>更新日志记录</h2>
        </div>
        <button class="ghost-button" :disabled="releaseNotesLoading" @click="loadReleaseNotes">刷新</button>
      </div>
      <article v-for="item in releaseNotes" :key="item.id" class="feedback-admin-card">
        <div class="feedback-admin-head">
          <div>
            <strong>{{ item.title }}</strong>
            <span>{{ item.is_published ? `已发布 · ${formatTime(item.published_at)}` : '草稿' }}</span>
          </div>
          <button class="ghost-button" :disabled="releaseNoteSaving" @click="editReleaseNote(item)">编辑</button>
        </div>
        <div class="feedback-admin-content">
          <p>{{ item.content }}</p>
        </div>
      </article>
      <p v-if="!releaseNotes.length" class="panel-subcopy">暂无更新日志。</p>
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

.release-note-admin-panel {
  display: grid;
  gap: 14px;
}

.release-note-form {
  border: 0;
  padding: 0;
  background: transparent;
}

.release-note-publish-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
}

@media (max-width: 780px) {
  .feedback-admin-head {
    grid-template-columns: 1fr;
  }
}
</style>
