<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api } from '../utils/api'

interface UserRow {
  id: number
  username: string
  full_name: string
  role: string
  grade?: number | null
  grade_label?: string | null
  is_graduated: boolean
  is_active: boolean
  classroom_name?: string | null
  classroom_label?: string | null
  must_change_password: boolean
}

interface ClassroomOption {
  id: number
  grade: number
  name: string
  label: string
}

interface ImportIssue {
  row_number: number
  full_name?: string | null
  login_account?: string | null
  reason: string
}

interface ImportResult {
  rows: number
  created: number
  skipped_existing: number
  invalid: number
  issues: ImportIssue[]
}

type GradeStatus = 'unset' | '1' | '2' | '3' | 'graduated'

const users = ref<UserRow[]>([])
const classroomOptions = ref<ClassroomOption[]>([])
const importResult = ref<ImportResult | null>(null)
const usersLoading = ref(false)
const classroomsLoading = ref(false)
const gradeOptions = [
  { label: '未设置', value: 'unset' },
  { label: '高一', value: '1' },
  { label: '高二', value: '2' },
  { label: '高三', value: '3' },
  { label: '毕业', value: 'graduated' },
] as const
const form = reactive({
  full_name: '',
  role: 'teacher',
  classroom_name: '',
  grade_status: 'unset' as GradeStatus,
})
const filters = reactive({
  keyword: '',
  grade: '',
  classroom_name: '',
})
const editDialogVisible = ref(false)
const editForm = reactive({
  id: 0,
  role: 'student',
  full_name: '',
  classroom_name: '',
  grade_status: 'unset' as GradeStatus,
  is_active: true,
})

async function loadUsers() {
  usersLoading.value = true
  try {
    const params: Record<string, string> = {}
    if (filters.keyword.trim()) {
      params.keyword = filters.keyword.trim()
    }
    if (filters.grade) {
      params.grade = filters.grade
    }
    if (filters.classroom_name.trim()) {
      params.classroom_name = filters.classroom_name.trim()
    }
    const { data } = await api.get<UserRow[]>('/admin/users', { params })
    users.value = data
  } finally {
    usersLoading.value = false
  }
}

async function loadClassrooms() {
  classroomsLoading.value = true
  try {
    const params: Record<string, string> = {}
    if (filters.grade) {
      params.grade = filters.grade
    }
    const { data } = await api.get<ClassroomOption[]>('/admin/classrooms', { params })
    classroomOptions.value = data
    if (filters.classroom_name && !data.some((item) => item.name === filters.classroom_name)) {
      filters.classroom_name = ''
    }
  } finally {
    classroomsLoading.value = false
  }
}

async function handleGradeFilterChange() {
  filters.classroom_name = ''
  await loadClassrooms()
  await loadUsers()
}

async function resetFilters() {
  filters.keyword = ''
  filters.grade = ''
  filters.classroom_name = ''
  await loadClassrooms()
  await loadUsers()
}

function parseGradePayload(gradeStatus: GradeStatus) {
  if (gradeStatus === 'graduated') {
    return { grade: null, is_graduated: true }
  }
  if (gradeStatus === 'unset') {
    return { grade: null, is_graduated: false }
  }
  return { grade: Number(gradeStatus), is_graduated: false }
}

function userGradeStatus(user: UserRow): GradeStatus {
  if (user.is_graduated) {
    return 'graduated'
  }
  if (user.grade === 1 || user.grade === 2 || user.grade === 3) {
    return String(user.grade) as GradeStatus
  }
  return 'unset'
}

async function createUser() {
  const payload = {
    full_name: form.full_name,
    role: form.role,
    classroom_name: form.role === 'student' ? form.classroom_name : null,
    ...parseGradePayload(form.grade_status),
  }
  await api.post('/admin/users', payload)
  ElMessage.success('用户已创建，默认密码为：姓名拼音+123456')
  Object.assign(form, {
    full_name: '',
    role: 'teacher',
    classroom_name: '',
    grade_status: 'unset',
  })
  await Promise.all([loadUsers(), loadClassrooms()])
}

async function importStudents(file: File) {
  const formData = new FormData()
  formData.append('file', file)
  const { data } = await api.post<ImportResult>('/admin/users/import', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  importResult.value = data
  ElMessage.success(`批量创建已处理：新增 ${data.created}，跳过 ${data.skipped_existing}，异常 ${data.invalid}`)
  await Promise.all([loadUsers(), loadClassrooms()])
}

async function resetPassword(userId: number) {
  await api.post('/admin/users/reset-password', { user_id: userId })
  ElMessage.success('密码已重置为默认密码：姓名拼音+123456')
  await loadUsers()
}

function openEditDialog(user: UserRow) {
  Object.assign(editForm, {
    id: user.id,
    role: user.role,
    full_name: user.full_name,
    classroom_name: user.classroom_name || '',
    grade_status: userGradeStatus(user),
    is_active: user.is_active,
  })
  editDialogVisible.value = true
}

async function saveUserEdit() {
  const payload = {
    full_name: editForm.full_name,
    role: editForm.role,
    classroom_name: editForm.role === 'student' ? editForm.classroom_name || null : null,
    is_active: editForm.is_active,
    ...(editForm.role === 'student' ? parseGradePayload(editForm.grade_status) : { grade: null, is_graduated: false }),
  }
  await api.put(`/admin/users/${editForm.id}`, payload)
  ElMessage.success('用户信息已更新；如姓名或班级变化，登录账号会自动重算')
  editDialogVisible.value = false
  await Promise.all([loadUsers(), loadClassrooms()])
}

async function deleteUser(user: UserRow) {
  try {
    await ElMessageBox.confirm(`确认删除 ${user.full_name}（${user.username}）？`, '删除用户', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      customClass: 'user-delete-confirm',
      confirmButtonClass: 'user-delete-confirm__confirm',
      cancelButtonClass: 'user-delete-confirm__cancel',
    })
    await api.delete(`/admin/users/${user.id}`)
    ElMessage.success('用户已删除')
    await loadUsers()
  } catch (error) {
    const detail = (error as { response?: { data?: { detail?: string } } }).response?.data?.detail
    if (detail) {
      ElMessage.error(detail)
    }
  }
}

function formatImportReason(reason: string) {
  const mapping: Record<string, string> = {
    missing_full_name: '缺少姓名',
    invalid_role: '身份无效，请填写学生/教师',
    invalid_grade: '年级格式无效',
    duplicate_login_account_in_file: '文件内生成的登录账号重复',
    login_account_already_exists: '登录账号已存在',
    'Student classroom is required': '学生必须填写班级',
    'Student grade must be 高一/高二/高三 or 毕业': '学生必须填写高一/高二/高三',
    'Classroom name must contain a class number': '班级需要带班级数字，如 3班',
  }
  return mapping[reason] || reason
}

onMounted(async () => {
  await Promise.all([loadUsers(), loadClassrooms()])
})
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">User Operations</p>
          <h2>创建用户与批量导入</h2>
          <p class="panel-subcopy">
            登录账号自动为姓名拼音 + 班级数字（教师无班级后缀）；默认密码为姓名拼音 + 123456。
          </p>
        </div>
      </div>
      <div class="user-form-grid">
        <el-input v-model="form.full_name" placeholder="姓名" />
        <el-select v-model="form.role">
          <el-option label="教师" value="teacher" />
          <el-option label="学生" value="student" />
        </el-select>
        <el-input v-if="form.role === 'student'" v-model="form.classroom_name" placeholder="班级，如 3班" />
        <el-select v-if="form.role === 'student'" v-model="form.grade_status" placeholder="学生年级">
          <el-option v-for="item in gradeOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
      </div>
      <div class="toolbar">
        <button class="primary-button" @click="createUser">创建用户</button>
        <el-upload :show-file-list="false" :auto-upload="false" :on-change="(file) => importStudents(file.raw!)">
          <button class="ghost-button">批量创建 CSV / XLSX</button>
        </el-upload>
      </div>
      <div class="task-card">
        <div class="task-card-head">
          <strong>批量导入格式说明</strong>
        </div>
        <div class="dashboard-stack">
          <p class="panel-subcopy">
            仅支持 <code>csv</code> 和 <code>xlsx</code>。表头可使用英文列名或中文列名。
          </p>
          <div class="table-like">
            <article class="table-row table-row-wrap">
              <strong>必填列</strong>
              <span><code>full_name</code> / <code>姓名</code></span>
              <span><code>role</code> / <code>身份</code></span>
            </article>
            <article class="table-row table-row-wrap">
              <strong>学生额外必填</strong>
              <span><code>class_name</code> / <code>班级</code></span>
              <span><code>grade</code> / <code>年级</code></span>
            </article>
            <article class="table-row table-row-wrap">
              <strong>身份取值</strong>
              <span><code>student</code> / 学生</span>
              <span><code>teacher</code> / 教师</span>
            </article>
            <article class="table-row table-row-wrap">
              <strong>学生年级取值</strong>
              <span>仅支持 <code>1</code> / <code>2</code> / <code>3</code></span>
              <span>分别表示高一 / 高二 / 高三</span>
            </article>
            <article class="table-row table-row-wrap">
              <strong>班级格式</strong>
              <span>班级名必须带班级数字</span>
              <span>示例：<code>3班</code>、<code>高一（2）班</code></span>
            </article>
            <article class="table-row table-row-wrap">
              <strong>教师规则</strong>
              <span>教师不使用年级和班级列</span>
              <span>填写了也会被忽略</span>
            </article>
            <article class="table-row table-row-wrap">
              <strong>账号与密码</strong>
              <span>登录账号自动生成为姓名拼音</span>
              <span>学生会追加班级数字后缀；默认密码为姓名拼音 + <code>123456</code></span>
            </article>
          </div>
          <div class="table-like">
            <article class="table-row table-row-wrap">
              <strong>CSV / XLSX 示例</strong>
              <span><code>full_name,role,class_name,grade</code></span>
              <span><code>张三,student,3班,1</code></span>
              <span><code>李四,teacher,,</code></span>
            </article>
          </div>
        </div>
      </div>
      <div v-if="importResult" class="task-card">
        <div class="task-card-head">
          <strong>最近导入反馈</strong>
          <span>{{ importResult.rows }} 行</span>
        </div>
        <div class="detail-chip-group">
          <span class="detail-chip">新增 {{ importResult.created }}</span>
          <span class="detail-chip">跳过 {{ importResult.skipped_existing }}</span>
          <span class="detail-chip">异常 {{ importResult.invalid }}</span>
        </div>
        <div v-if="importResult.issues.length" class="table-like">
          <article v-for="item in importResult.issues" :key="`${item.row_number}-${item.login_account || item.full_name || 'none'}`" class="table-row table-row-wrap">
            <strong>第 {{ item.row_number }} 行</strong>
            <span>{{ item.full_name || '无姓名' }}</span>
            <span>{{ item.login_account || '未生成账号' }}</span>
            <span>{{ formatImportReason(item.reason) }}</span>
          </article>
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">User Table</p>
          <h2>当前账号</h2>
        </div>
        <div class="toolbar">
          <button class="ghost-button" :disabled="usersLoading" @click="loadUsers">刷新</button>
        </div>
      </div>
      <div class="user-form-grid user-filter-grid">
        <el-input
          v-model="filters.keyword"
          clearable
          placeholder="按姓名搜索"
          @keyup.enter="loadUsers"
          @clear="loadUsers"
        />
        <el-select v-model="filters.grade" clearable placeholder="按年级筛选" @change="handleGradeFilterChange" @clear="handleGradeFilterChange">
          <el-option label="未设置" value="unset" />
          <el-option label="高一" value="1" />
          <el-option label="高二" value="2" />
          <el-option label="高三" value="3" />
          <el-option label="毕业" value="graduated" />
        </el-select>
        <el-select
          v-model="filters.classroom_name"
          clearable
          filterable
          :disabled="classroomsLoading || !classroomOptions.length"
          placeholder="按班级筛选"
          @change="loadUsers"
          @clear="loadUsers"
        >
          <el-option v-for="item in classroomOptions" :key="item.id" :label="item.label" :value="item.name" />
        </el-select>
        <div class="toolbar">
          <button class="primary-button" :disabled="usersLoading" @click="loadUsers">筛选</button>
          <button class="ghost-button" :disabled="usersLoading" @click="resetFilters">清空</button>
        </div>
      </div>
      <article v-for="item in users" :key="item.id" class="table-row">
        <strong>{{ item.full_name }}</strong>
        <span>{{ item.username }}</span>
        <span>{{ item.role === 'student' ? '学生' : item.role === 'teacher' ? '教师' : item.role }}</span>
        <span>{{ item.grade_label || '-' }}</span>
        <span>{{ item.classroom_label || '-' }}</span>
        <span>{{ item.is_active ? '启用' : '停用' }}</span>
        <span>{{ item.must_change_password ? '需改密' : '正常' }}</span>
        <button class="ghost-button" @click="openEditDialog(item)">编辑</button>
        <button class="ghost-button" @click="resetPassword(item.id)">重置密码</button>
        <button class="ghost-button danger-text" @click="deleteUser(item)">删除</button>
      </article>
      <p v-if="!users.length" class="panel-subcopy">暂无匹配账号。</p>
    </section>
  </section>

  <el-dialog v-model="editDialogVisible" title="编辑用户" width="480px">
    <div class="dashboard-stack">
      <el-input v-model="editForm.full_name" placeholder="姓名" />
      <el-select v-model="editForm.role" placeholder="身份">
        <el-option label="教师" value="teacher" />
        <el-option label="学生" value="student" />
      </el-select>
      <el-input v-if="editForm.role === 'student'" v-model="editForm.classroom_name" placeholder="班级，如 3班" />
      <el-select v-if="editForm.role === 'student'" v-model="editForm.grade_status" placeholder="学生年级">
        <el-option v-for="item in gradeOptions" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>
      <el-switch
        v-model="editForm.is_active"
        inline-prompt
        active-text="启用"
        inactive-text="停用"
      />
    </div>
    <template #footer>
      <button class="ghost-button" @click="editDialogVisible = false">取消</button>
      <button class="primary-button" @click="saveUserEdit">保存</button>
    </template>
  </el-dialog>
</template>
