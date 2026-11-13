<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '../utils/api'

interface SubjectAvailability {
  available_subjects: string[]
  enabled_subjects: string[]
}

const subjects = ref<string[]>([])
const enabled = ref<string[]>([])
const saved = ref<string[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const changed = computed(() =>
  enabled.value.length !== saved.value.length || enabled.value.some((subject) => !saved.value.includes(subject)),
)

function applySettings(data: SubjectAvailability) {
  subjects.value = data.available_subjects
  enabled.value = [...data.enabled_subjects]
  saved.value = data.enabled_subjects
}

async function loadSettings() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get<SubjectAvailability>('/admin/subjects')
    applySettings(data)
  } catch {
    error.value = '暂时无法读取学科设置。'
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  saving.value = true
  try {
    const { data } = await api.put<SubjectAvailability>('/admin/subjects', { enabled_subjects: enabled.value })
    applySettings(data)
    ElMessage.success('学科设置已保存')
  } catch {
    ElMessage.error('学科设置保存失败，请重试。')
  } finally {
    saving.value = false
  }
}

onMounted(loadSettings)
</script>

<template>
  <section class="panel" v-loading="loading">
    <div class="panel-header panel-header--wrap">
      <div>
        <h2>学科启用</h2>
        <p class="panel-subcopy">学生只能选择已勾选的学科。停用学科后，原有答疑记录仍可查看。</p>
      </div>
      <button
        v-if="!error"
        type="button"
        class="primary-button"
        :disabled="loading || saving || !changed"
        @click="saveSettings"
      >
        {{ saving ? '保存中…' : '保存学科设置' }}
      </button>
    </div>
    <div v-if="error" class="toolbar">
      <span role="status">{{ error }}</span>
      <button type="button" class="ghost-button" @click="loadSettings">重试</button>
    </div>
    <template v-else-if="!loading">
      <el-checkbox-group v-model="enabled" :disabled="saving" aria-label="启用的学科">
        <el-checkbox v-for="subject in subjects" :key="subject" :value="subject" :label="subject" />
      </el-checkbox-group>
      <p v-if="!enabled.length" class="panel-subcopy" role="status">未勾选任何学科，保存后学生将无法发起答疑。</p>
    </template>
  </section>
</template>
