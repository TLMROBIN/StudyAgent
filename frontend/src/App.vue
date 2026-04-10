<script setup lang="ts">
import { computed, ref, watch, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from './stores/auth'
import { forceLoginRedirect } from './utils/navigation'

const SIDEBAR_COLLAPSED_STORAGE_KEY = 'studyagent-sidebar-collapsed'

interface NavigationItem {
  to: string
  label: string
  shortLabel: string
}

function readSidebarCollapsed() {
  if (typeof window === 'undefined') {
    return false
  }
  return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === '1'
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sidebarCollapsed = ref(readSidebarCollapsed())

const requiresAuth = computed(() => route.matched.some((record) => record.meta.requiresAuth))
const showShell = computed(() => route.path !== '/login')
const shellActive = computed(() => showShell.value && !!auth.user)
const routeReady = computed(() => !requiresAuth.value || auth.initialized)
const navigationItems = computed<NavigationItem[]>(() => {
  const items: NavigationItem[] = []

  if (auth.user?.role === 'student') {
    items.push({ to: '/student', label: '学生答疑', shortLabel: '答疑' })
  }

  if (auth.user?.role !== 'student') {
    items.push(
      { to: '/admin', label: '统计看板', shortLabel: '看板' },
      { to: '/admin/knowledge', label: '知识库', shortLabel: '知识' },
    )
  }

  if (auth.user?.role === 'admin') {
    items.push(
      { to: '/admin/audit', label: '审计日志', shortLabel: '审计' },
      { to: '/admin/agent', label: '智能体配置', shortLabel: '配置' },
      { to: '/admin/users', label: '用户管理', shortLabel: '用户' },
    )
  }

  return items
})
const sidebarUserBadge = computed(() => {
  const name = auth.user?.full_name?.trim() || ''
  if (!name) {
    return '用户'
  }
  return name.slice(Math.max(0, name.length - 2))
})

watchEffect(() => {
  if (auth.initialized && requiresAuth.value && !auth.user && route.path !== '/login') {
    forceLoginRedirect()
  }
})

watch(sidebarCollapsed, (value) => {
  if (typeof window === 'undefined') {
    return
  }
  window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, value ? '1' : '0')
})

async function handleLogout() {
  await auth.logout()
  router.push('/login')
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
}
</script>

<template>
  <div
    :class="[
      'app-root',
      {
        'app-root--shell': shellActive,
        'app-root--plain': !shellActive,
        'app-root--sidebar-collapsed': shellActive && sidebarCollapsed,
      },
    ]"
  >
    <div class="ambient ambient-one"></div>
    <div class="ambient ambient-two"></div>
    <aside v-if="shellActive" :class="['app-sidebar', { 'app-sidebar--collapsed': sidebarCollapsed }]">
      <div class="sidebar-body">
        <div class="sidebar-head">
          <div class="sidebar-brand">
            <p class="eyebrow">StudyAgent</p>
            <h1 class="brand-title">{{ sidebarCollapsed ? '答疑' : '高中学科答疑' }}</h1>
            <p v-if="!sidebarCollapsed" class="brand-copy">检索增强 + 苏格拉底引导，先帮学生想清楚，再帮助学生做出来。</p>
          </div>
          <button class="sidebar-toggle" @click="toggleSidebar">
            {{ sidebarCollapsed ? '展开' : '收起' }}
          </button>
        </div>
        <nav class="nav-list">
          <RouterLink
            v-for="item in navigationItems"
            :key="item.to"
            :to="item.to"
            :title="item.label"
            :aria-label="item.label"
          >
            <span class="nav-link__short">{{ item.shortLabel }}</span>
            <span v-if="!sidebarCollapsed" class="nav-link__label">{{ item.label }}</span>
          </RouterLink>
        </nav>
      </div>
      <div class="sidebar-footer">
        <div class="profile-chip">
          <strong>{{ sidebarCollapsed ? sidebarUserBadge : auth.user.full_name }}</strong>
          <span v-if="!sidebarCollapsed">{{ auth.user.role }}</span>
        </div>
        <button class="ghost-button sidebar-logout" @click="handleLogout">
          {{ sidebarCollapsed ? '退出' : '退出登录' }}
        </button>
      </div>
    </aside>
    <main :class="['app-main', { 'app-main--plain': !shellActive }]">
      <div v-if="!routeReady" class="auth-guard">
        <p class="panel-subcopy">正在检查登录状态...</p>
      </div>
      <RouterView v-else />
    </main>
  </div>
</template>
