<script setup lang="ts">
import { reactive, ref } from 'vue'
import axios from 'axios'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const mode = ref<'student' | 'staff'>('student')
const studentForm = reactive({ username: '', password: '' })
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
      await auth.loginStudent(studentForm.username, studentForm.password)
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
        StudyAgent 你的专属苏格拉底式导师，结合知识库与多轮引导，帮助你一步步形成思路，锻炼思维能力，建立完整的知识体系。
      </p>
      <ul class="feature-list">
        <li>不限量问答，支持相关题目推荐匹配知识点、年级、难易度。</li>
        <li>面向平板设备设计，适配局域网部署，不依赖外部网络。</li>
      </ul>
      <p class="login-note login-note--hero">目前仅具备物理知识库与题库，其他学科等待完善中</p>
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
        <el-form-item label="登录账号">
          <el-input v-model="studentForm.username" placeholder="输入登录账号" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="studentForm.password" show-password placeholder="输入密码" />
        </el-form-item>
        <p class="login-note">初始账号：姓名全拼+班级号；初始密码姓名全拼+123456</p>
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
