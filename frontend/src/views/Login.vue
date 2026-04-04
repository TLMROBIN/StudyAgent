<script setup lang="ts">
import { reactive, ref } from 'vue'
import axios from 'axios'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const mode = ref<'student' | 'staff'>('student')
const studentForm = reactive({ studentNo: '', password: '' })
const staffForm = reactive({ username: '', password: '' })
const loading = ref(false)

function resolveLoginError(error: unknown) {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (!error.response) {
      return '后端服务未连接，请稍后重试'
    }
    if (error.response.status === 401) {
      return '账号或密码错误'
    }
    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }
  }
  return '登录失败，请检查账号和密码'
}

async function submit() {
  loading.value = true
  try {
    if (mode.value === 'student') {
      await auth.loginStudent(studentForm.studentNo, studentForm.password)
      router.push('/student')
    } else {
      await auth.loginStaff(staffForm.username, staffForm.password)
      router.push('/admin')
    }
  } catch (error) {
    console.error(error)
    ElMessage.error(resolveLoginError(error))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section class="login-page">
    <div class="login-hero">
      <p class="eyebrow">LAN Classroom AI</p>
      <h1>让答疑更像一场耐心的推理训练</h1>
      <p>
        StudyAgent 先过滤非学科问题，再结合知识库与多轮引导，帮助学生把思路一步步搭出来。
      </p>
      <ul class="feature-list">
        <li>支持学生历史记录与已解决标记</li>
        <li>管理端可查看统计、上传资料、调整智能体策略</li>
        <li>面向平板设备设计，适配局域网部署</li>
      </ul>
    </div>
    <div class="login-card">
      <div class="login-card-head">
        <p class="eyebrow">Secure Access</p>
        <h2>{{ mode === 'student' ? '学生登录' : '教师 / 管理员登录' }}</h2>
        <p>登录后可继续使用答疑、知识库与管理功能。</p>
      </div>
      <el-segmented
        v-model="mode"
        class="login-mode-switch"
        :options="[{ label: '学生登录', value: 'student' }, { label: '教师 / 管理员', value: 'staff' }]"
      />
      <el-form v-if="mode === 'student'" class="login-form" @submit.prevent="submit">
        <el-form-item label="学号">
          <el-input v-model="studentForm.studentNo" placeholder="输入学号" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="studentForm.password" show-password placeholder="输入密码" />
        </el-form-item>
      </el-form>
      <el-form v-else class="login-form" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input v-model="staffForm.username" placeholder="输入用户名" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="staffForm.password" show-password placeholder="输入密码" />
        </el-form-item>
      </el-form>
      <button class="primary-button login-submit" :disabled="loading" @click="submit">
        {{ loading ? '登录中...' : '进入系统' }}
      </button>
    </div>
  </section>
</template>
