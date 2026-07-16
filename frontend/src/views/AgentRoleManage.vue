<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api } from '../utils/api'

const SUBJECTS = ['语文', '数学', '英语', '物理', '化学', '生物', '政治', '历史', '地理']
const TONES = [
  { value: 'warm', label: '亲切耐心' },
  { value: 'rigorous', label: '严谨克制' },
  { value: 'humorous', label: '轻松幽默' },
  { value: 'calm', label: '沉稳平和' },
  { value: 'poetic', label: '富有画面' },
]
const PACES = [
  { value: 'concise_steps', label: '短步骤推进' },
  { value: 'guided_questions', label: '循序追问' },
  { value: 'intuition_then_concept', label: '先直觉后概念' },
  { value: 'example_then_summary', label: '先例子后归纳' },
]
const ANALOGIES = [
  { value: 'daily_life', label: '日常生活' },
  { value: 'experiment', label: '实验情境' },
  { value: 'thought_experiment', label: '思想实验' },
  { value: 'literary_imagery', label: '文学意象' },
  { value: 'historical_context', label: '历史情境' },
  { value: 'minimal', label: '尽量少用类比' },
]
const TRAITS = [
  { value: 'simple_analogies', label: '简单类比' },
  { value: 'student_restate', label: '请学生复述' },
  { value: 'evidence_first', label: '证据优先' },
  { value: 'thought_experiments', label: '思想实验' },
  { value: 'literary_imagery', label: '文学画面' },
  { value: 'concise_language', label: '简洁表达' },
  { value: 'gentle_humor', label: '温和幽默' },
]

interface RoleStyleConfig {
  tone: string
  explanation_pace: string
  analogy_style: string
  formality: string
  sentence_length: string
  traits: string[]
}

interface RoleRevision {
  id: number
  revision: number
  style_config: RoleStyleConfig
  renderer_version: string
  content_hash: string
  created_at: string
}

interface AgentRole {
  id: number
  name: string
  display_name: string
  emoji?: string | null
  description: string
  subjects?: string[] | null
  is_enabled: boolean
  sort_order: number
  current_revision: RoleRevision
  created_at: string
  updated_at: string
}

const roles = ref<AgentRole[]>([])
const revisions = ref<RoleRevision[]>([])
const editingId = ref<number | null>(null)
const saving = ref(false)
const loading = ref(false)
const revisionRoleId = ref<number | null>(null)

const form = reactive({
  name: '',
  display_name: '',
  emoji: '',
  description: '',
  subjects: [] as string[],
  sort_order: 0,
  style_config: {
    tone: 'warm',
    explanation_pace: 'guided_questions',
    analogy_style: 'daily_life',
    formality: 'natural',
    sentence_length: 'medium',
    traits: [] as string[],
  },
})

const formTitle = computed(() => editingId.value ? `编辑“${form.display_name}”` : '创建教学角色')

function resetForm() {
  editingId.value = null
  form.name = ''
  form.display_name = ''
  form.emoji = ''
  form.description = ''
  form.subjects = []
  form.sort_order = 0
  form.style_config = {
    tone: 'warm',
    explanation_pace: 'guided_questions',
    analogy_style: 'daily_life',
    formality: 'natural',
    sentence_length: 'medium',
    traits: [],
  }
}

async function loadRoles() {
  loading.value = true
  try {
    const { data } = await api.get<AgentRole[]>('/agent-roles/')
    roles.value = data
  } finally {
    loading.value = false
  }
}

function editRole(role: AgentRole) {
  editingId.value = role.id
  form.name = role.name
  form.display_name = role.display_name
  form.emoji = role.emoji || ''
  form.description = role.description
  form.subjects = [...(role.subjects || [])]
  form.sort_order = role.sort_order
  form.style_config = {
    ...role.current_revision.style_config,
    traits: [...role.current_revision.style_config.traits],
  }
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

async function saveRole() {
  if (!form.name.trim() || !form.display_name.trim()) {
    ElMessage.info('请填写角色标识和显示名称')
    return
  }
  saving.value = true
  const payload = {
    display_name: form.display_name,
    emoji: form.emoji || null,
    description: form.description,
    subjects: form.subjects.length ? form.subjects : null,
    sort_order: form.sort_order,
    style_config: form.style_config,
  }
  try {
    if (editingId.value) {
      await api.put(`/agent-roles/${editingId.value}`, payload)
      ElMessage.success('角色已更新；风格变化会自动生成新修订版')
    } else {
      await api.post('/agent-roles/', { name: form.name, ...payload })
      ElMessage.success('角色已创建，启用后学生才能选择')
    }
    resetForm()
    await loadRoles()
  } catch (error) {
    const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail || '保存失败，请检查填写内容')
  } finally {
    saving.value = false
  }
}

async function toggleRole(role: AgentRole) {
  try {
    await api.put(`/agent-roles/${role.id}/enabled`, { is_enabled: !role.is_enabled })
    ElMessage.success(role.is_enabled ? '角色已停用' : '角色已启用')
    await loadRoles()
  } catch {
    ElMessage.error('角色状态更新失败')
  }
}

async function importDefaults() {
  try {
    await ElMessageBox.confirm('导入费曼、爱因斯坦、莎士比亚三个内置角色？导入后默认保持停用。', '导入内置角色', {
      confirmButtonText: '导入',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch {
    return
  }
  const { data } = await api.post<{ created: number; skipped: number }>('/agent-roles/import-defaults')
  ElMessage.success(`已创建 ${data.created} 个，跳过 ${data.skipped} 个已有角色`)
  await loadRoles()
}

async function showRevisions(role: AgentRole) {
  if (revisionRoleId.value === role.id) {
    revisionRoleId.value = null
    revisions.value = []
    return
  }
  const { data } = await api.get<RoleRevision[]>(`/agent-roles/${role.id}/revisions`)
  revisionRoleId.value = role.id
  revisions.value = data
}

onMounted(loadRoles)
</script>

<template>
  <section class="dashboard-stack role-manager">
    <section class="panel role-editor">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Teaching Style</p>
          <h2>{{ formTitle }}</h2>
          <p class="panel-subcopy">角色只控制表达方式，不会改变苏格拉底教学阶段、安全规则或模型参数。</p>
        </div>
        <button v-if="editingId" class="ghost-button" @click="resetForm">取消编辑</button>
      </div>

      <div class="role-form-grid">
        <label>
          角色标识
          <el-input v-model="form.name" :disabled="Boolean(editingId)" placeholder="如 feynman" />
          <small>创建后不可修改，仅使用小写字母、数字、短横线或下划线。</small>
        </label>
        <label>
          显示名称
          <el-input v-model="form.display_name" placeholder="如 费曼老师" />
        </label>
        <label>
          表情
          <el-input v-model="form.emoji" maxlength="16" placeholder="可选，如 🧠" />
        </label>
        <label>
          排序
          <el-input-number v-model="form.sort_order" :min="-10000" :max="10000" controls-position="right" />
        </label>
        <label class="role-form-grid__wide">
          简介
          <el-input v-model="form.description" maxlength="255" show-word-limit placeholder="给学生看的简短说明" />
        </label>
        <label class="role-form-grid__wide">
          适用学科
          <el-select v-model="form.subjects" multiple collapse-tags collapse-tags-tooltip placeholder="留空表示全部学科">
            <el-option v-for="subject in SUBJECTS" :key="subject" :label="subject" :value="subject" />
          </el-select>
        </label>
      </div>

      <div class="role-style-section">
        <h3>结构化表达风格</h3>
        <div class="role-form-grid role-form-grid--style">
          <label>语气<el-select v-model="form.style_config.tone"><el-option v-for="item in TONES" :key="item.value" v-bind="item" /></el-select></label>
          <label>讲解节奏<el-select v-model="form.style_config.explanation_pace"><el-option v-for="item in PACES" :key="item.value" v-bind="item" /></el-select></label>
          <label>类比方式<el-select v-model="form.style_config.analogy_style"><el-option v-for="item in ANALOGIES" :key="item.value" v-bind="item" /></el-select></label>
          <label>措辞正式度<el-select v-model="form.style_config.formality"><el-option label="对话自然" value="conversational" /><el-option label="自然规范" value="natural" /><el-option label="正式严谨" value="formal" /></el-select></label>
          <label>句子长度<el-select v-model="form.style_config.sentence_length"><el-option label="短句为主" value="short" /><el-option label="长度适中" value="medium" /><el-option label="长短结合" value="varied" /></el-select></label>
          <label class="role-form-grid__wide">附加特征（最多 3 项）<el-select v-model="form.style_config.traits" multiple :multiple-limit="3" collapse-tags><el-option v-for="item in TRAITS" :key="item.value" v-bind="item" /></el-select></label>
        </div>
      </div>

      <div class="action-row role-editor__actions">
        <button class="primary-button" :disabled="saving" @click="saveRole">{{ saving ? '保存中...' : editingId ? '保存修改' : '创建角色' }}</button>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <h2>角色列表</h2>
          <p class="panel-subcopy">停用角色不会出现在学生端；历史消息仍保留当时使用的修订信息。</p>
        </div>
        <button class="ghost-button" @click="importDefaults">导入内置角色</button>
      </div>

      <div v-if="loading" class="role-empty">正在加载角色…</div>
      <div v-else-if="!roles.length" class="role-empty">还没有教学角色。可以从上方创建，或导入三个内置示例。</div>
      <article v-for="role in roles" v-else :key="role.id" class="role-row">
        <div class="role-row__identity">
          <span class="role-row__emoji">{{ role.emoji || '◦' }}</span>
          <div>
            <div class="role-row__title">
              <strong>{{ role.display_name }}</strong>
              <span :class="['role-state', { 'role-state--enabled': role.is_enabled }]">{{ role.is_enabled ? '已启用' : '已停用' }}</span>
            </div>
            <p>{{ role.description || '暂无简介' }}</p>
            <small>{{ role.subjects?.length ? role.subjects.join('、') : '全部学科' }} · 修订 v{{ role.current_revision.revision }}</small>
          </div>
        </div>
        <div class="row-actions">
          <button class="ghost-button" @click="editRole(role)">编辑</button>
          <button class="ghost-button" @click="showRevisions(role)">{{ revisionRoleId === role.id ? '收起版本' : '版本记录' }}</button>
          <button :class="role.is_enabled ? 'ghost-button' : 'primary-button'" @click="toggleRole(role)">{{ role.is_enabled ? '停用' : '启用' }}</button>
        </div>
        <div v-if="revisionRoleId === role.id" class="role-revisions">
          <div v-for="revision in revisions" :key="revision.id" class="role-revision">
            <strong>v{{ revision.revision }}</strong>
            <span>{{ new Date(revision.created_at).toLocaleString() }}</span>
            <code>{{ revision.content_hash.slice(0, 12) }}</code>
          </div>
        </div>
      </article>
    </section>
  </section>
</template>

<style scoped>
.role-editor { max-width: 1120px; }
.role-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 20px; }
.role-form-grid label { display: flex; min-width: 0; flex-direction: column; gap: 7px; color: var(--ink); font-size: 13px; font-weight: 600; }
.role-form-grid small { color: var(--muted); font-weight: 400; line-height: 1.45; }
.role-form-grid__wide { grid-column: 1 / -1; }
.role-style-section { margin-top: 24px; padding-top: 20px; border-top: 1px solid rgba(15, 23, 42, 0.1); }
.role-style-section h3 { margin: 0 0 14px; font-size: 16px; }
.role-editor__actions { justify-content: flex-end; margin-top: 22px; }
.role-row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 0; border-top: 1px solid rgba(15, 23, 42, 0.09); }
.role-row__identity { display: flex; min-width: 280px; flex: 1 1 440px; gap: 14px; align-items: flex-start; }
.role-row__emoji { display: grid; width: 40px; height: 40px; flex: 0 0 40px; place-items: center; border-radius: 12px; background: rgba(15, 118, 110, 0.1); font-size: 21px; }
.role-row__title { display: flex; align-items: center; gap: 9px; }
.role-row p { margin: 5px 0; color: var(--ink); line-height: 1.5; }
.role-row small { color: var(--muted); }
.role-state { padding: 2px 8px; border-radius: 999px; background: rgba(100, 116, 139, 0.12); color: #475569; font-size: 11px; font-weight: 600; }
.role-state--enabled { background: rgba(15, 118, 110, 0.12); color: #0f5f58; }
.role-revisions { display: flex; width: 100%; flex-direction: column; gap: 7px; padding: 12px 14px; border-radius: 12px; background: rgba(15, 23, 42, 0.035); }
.role-revision { display: grid; grid-template-columns: 44px minmax(180px, 1fr) auto; gap: 12px; align-items: center; font-size: 12px; }
.role-revision code { color: var(--muted); }
.role-empty { padding: 34px 10px; color: var(--muted); text-align: center; }
@media (max-width: 720px) { .role-form-grid { grid-template-columns: 1fr; } .role-form-grid__wide { grid-column: auto; } .role-row .row-actions { width: 100%; } .role-revision { grid-template-columns: 40px 1fr; } .role-revision code { display: none; } }
</style>
