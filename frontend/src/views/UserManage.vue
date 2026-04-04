<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '../utils/api'

interface UserRow {
  id: number
  username: string
  full_name: string
  role: string
  grade?: number | null
  classroom_label?: string | null
  student_no?: string | null
  must_change_password: boolean
}

interface ImportIssue {
  row_number: number
  student_no?: string | null
  reason: string
}

interface ImportResult {
  rows: number
  created: number
  skipped_existing: number
  invalid: number
  issues: ImportIssue[]
}

const users = ref<UserRow[]>([])
const importResult = ref<ImportResult | null>(null)
const form = reactive({
  username: '',
  full_name: '',
  role: 'teacher',
  password: '',
  student_no: '',
})

async function loadUsers() {
  const { data } = await api.get<UserRow[]>('/admin/users')
  users.value = data
}

async function createUser() {
  await api.post('/admin/users', form)
  ElMessage.success('用户已创建')
  Object.assign(form, {
    username: '',
    full_name: '',
    role: 'teacher',
    password: '',
    student_no: '',
  })
  await loadUsers()
}

async function importStudents(file: File) {
  const formData = new FormData()
  formData.append('file', file)
  const { data } = await api.post<ImportResult>('/admin/students/import', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  importResult.value = data
  ElMessage.success(`学生名单已处理：新增 ${data.created}，跳过 ${data.skipped_existing}，异常 ${data.invalid}`)
  await loadUsers()
}

async function resetPassword(userId: number) {
  await api.post('/admin/users/reset-password', {
    user_id: userId,
    new_password: 'StudyAgent123',
  })
  ElMessage.success('密码已重置')
  await loadUsers()
}

function formatImportReason(reason: string) {
  const mapping: Record<string, string> = {
    missing_student_no: '缺少学号',
    duplicate_student_no_in_file: '文件内学号重复',
    student_already_exists: '学号已存在',
    invalid_grade: '年级格式无效',
  }
  return mapping[reason] || reason
}

onMounted(loadUsers)
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">User Operations</p>
          <h2>创建用户与批量导入</h2>
        </div>
      </div>
      <div class="user-form-grid">
        <el-input v-model="form.username" placeholder="用户名" />
        <el-input v-model="form.full_name" placeholder="姓名" />
        <el-select v-model="form.role">
          <el-option label="教师" value="teacher" />
          <el-option label="管理员" value="admin" />
          <el-option label="学生" value="student" />
        </el-select>
        <el-input v-model="form.student_no" placeholder="学号（学生可填）" />
        <el-input v-model="form.password" placeholder="初始密码" show-password />
      </div>
      <div class="toolbar">
        <button class="primary-button" @click="createUser">创建用户</button>
        <el-upload :show-file-list="false" :auto-upload="false" :on-change="(file) => importStudents(file.raw!)">
          <button class="ghost-button">导入 CSV / XLSX</button>
        </el-upload>
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
          <article v-for="item in importResult.issues" :key="`${item.row_number}-${item.student_no || 'none'}`" class="table-row table-row-wrap">
            <strong>第 {{ item.row_number }} 行</strong>
            <span>{{ item.student_no || '无学号' }}</span>
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
      </div>
      <article v-for="item in users" :key="item.id" class="table-row">
        <strong>{{ item.full_name }}</strong>
        <span>{{ item.username }}</span>
        <span>{{ item.role }}</span>
        <span>{{ item.student_no || '-' }}</span>
        <span>{{ item.classroom_label || '-' }}</span>
        <span>{{ item.must_change_password ? '需改密' : '正常' }}</span>
        <button class="ghost-button" @click="resetPassword(item.id)">重置密码</button>
      </article>
    </section>
  </section>
</template>
