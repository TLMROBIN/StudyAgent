<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '../utils/api'

interface AgentConfigItem {
  id: number
  version: number
  system_prompt: string
  guidance_params: Record<string, unknown>
  subject_prompts: Record<string, string>
  is_active: boolean
}

const SUBJECTS = ['语文', '数学', '英语', '物理', '化学', '生物', '政治', '历史', '地理']

const configs = ref<AgentConfigItem[]>([])
const form = reactive({
  system_prompt: '',
  max_questions_per_turn: 2,
  fallback_after_turns: 3,
})
// 每科追加提示词与可选 max_questions 覆盖（留空=继承全局）
const subjectPrompts = reactive<Record<string, string>>(
  Object.fromEntries(SUBJECTS.map((s) => [s, ''])),
)
const subjectMaxQuestions = reactive<Record<string, number | null>>(
  Object.fromEntries(SUBJECTS.map((s) => [s, null])),
)
const agentConfigCollectionPath = '/agent-config/'

async function loadConfigs() {
  const { data } = await api.get<AgentConfigItem[]>(agentConfigCollectionPath)
  configs.value = data
}

async function createConfig() {
  const guidanceParams: Record<string, unknown> = {
    max_questions_per_turn: form.max_questions_per_turn,
    fallback_after_turns: form.fallback_after_turns,
  }
  const bySubject: Record<string, Record<string, number>> = {}
  for (const subject of SUBJECTS) {
    const value = subjectMaxQuestions[subject]
    if (typeof value === 'number' && value >= 1 && value <= 3) {
      bySubject[subject] = { max_questions_per_turn: value }
    }
  }
  if (Object.keys(bySubject).length > 0) {
    guidanceParams.by_subject = bySubject
  }
  const promptsPayload: Record<string, string> = {}
  for (const subject of SUBJECTS) {
    const text = (subjectPrompts[subject] ?? '').trim()
    if (text) {
      promptsPayload[subject] = text
    }
  }
  await api.post(agentConfigCollectionPath, {
    system_prompt: form.system_prompt,
    guidance_params: guidanceParams,
    subject_prompts: promptsPayload,
    filter_rules: {},
  })
  form.system_prompt = ''
  for (const subject of SUBJECTS) {
    subjectPrompts[subject] = ''
    subjectMaxQuestions[subject] = null
  }
  ElMessage.success('已创建新版本')
  await loadConfigs()
}

async function activateConfig(id: number) {
  await api.post(`/agent-config/${id}/activate`)
  ElMessage.success('已切换生效版本')
  await loadConfigs()
}

onMounted(async () => {
  await loadConfigs()
})
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Prompt Versioning</p>
          <h2>智能体配置</h2>
        </div>
      </div>
      <el-input
        v-model="form.system_prompt"
        type="textarea"
        :rows="8"
        resize="none"
        placeholder="输入新版系统提示词（全局）"
      />
      <div class="param-row">
        <label>
          每轮最多引导问题数
          <el-input-number v-model="form.max_questions_per_turn" :min="1" :max="3" />
        </label>
        <label>
          兜底分步阶段轮次
          <el-input-number v-model="form.fallback_after_turns" :min="1" :max="6" />
        </label>
      </div>
      <el-collapse class="subject-collapse">
        <el-collapse-item
          v-for="subject in SUBJECTS"
          :key="subject"
          :title="`${subject} 专项配置`"
          :name="subject"
        >
          <el-input
            v-model="subjectPrompts[subject]"
            type="textarea"
            :rows="4"
            resize="none"
            :placeholder="`${subject} 追加提示词（可选，留空则不追加）`"
          />
          <label class="subject-param">
            该科每轮引导问题数（留空继承全局）
            <el-input-number
              v-model="subjectMaxQuestions[subject]"
              :min="1"
              :max="3"
              controls-position="right"
            />
          </label>
        </el-collapse-item>
      </el-collapse>
      <button class="primary-button" @click="createConfig">创建新版本</button>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Version List</p>
          <h2>配置版本</h2>
        </div>
      </div>
      <article v-for="item in configs" :key="item.id" class="version-card">
        <div>
          <strong>v{{ item.version }}</strong>
          <span>{{ item.is_active ? '当前生效' : '未启用' }}</span>
        </div>
        <p>{{ item.system_prompt }}</p>
        <p v-if="Object.keys(item.subject_prompts ?? {}).length" class="subject-tags">
          分学科提示词：{{ Object.keys(item.subject_prompts ?? {}).join('、') }}
        </p>
        <button class="ghost-button" :disabled="item.is_active" @click="activateConfig(item.id)">设为生效版本</button>
      </article>
    </section>
  </section>
</template>

<style scoped>
.param-row {
  display: flex;
  gap: 24px;
  margin: 12px 0;
  flex-wrap: wrap;
}
.param-row label,
.subject-param {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--el-text-color-regular, #606266);
}
.subject-collapse {
  margin: 12px 0;
}
.subject-param {
  margin-top: 10px;
}
.subject-tags {
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
}
</style>
