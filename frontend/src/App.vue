<script setup lang="ts">
import { computed, ref, watch, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from './stores/auth'
import { fetchFeedbackUnreadSummary, fetchIncentiveSummary, type FeedbackUnreadSummary } from './utils/api'
import { forceLoginRedirect } from './utils/navigation'

const SIDEBAR_COLLAPSED_STORAGE_KEY = 'studyagent-sidebar-collapsed'

interface NavigationItem {
  to: string
  label: string
  shortLabel: string
  showUnreadDot?: boolean
}

function readSidebarCollapsed() {
  if (typeof window === 'undefined') {
    return false
  }
  const saved = window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)
  return saved === null ? window.location.pathname.includes('/student') : saved === '1'
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sidebarCollapsed = ref(readSidebarCollapsed())
const unreadSummary = ref<FeedbackUnreadSummary>({
  unread_feedback_replies: 0,
  unread_release_notes: 0,
  has_unread: false,
})
const hasUnreadPraise = ref(false)

const requiresAuth = computed(() => route.matched.some((record) => record.meta.requiresAuth))
const showShell = computed(() => route.path !== '/login')
const shellActive = computed(() => showShell.value && !!auth.user)
const studentChatActive = computed(() => route.path === '/student')
const sidebarIsCollapsed = computed(() => sidebarCollapsed.value)
const chatView = ref<{ openHistory: () => void; newQuestion: () => void; openNotices: () => void } | null>(null)
const routeReady = computed(() => !requiresAuth.value || auth.initialized)
const navigationItems = computed<NavigationItem[]>(() => {
  const items: NavigationItem[] = []

  if (auth.user?.role === 'student') {
    items.push({ to: '/student', label: '学生答疑', shortLabel: '答疑' })
    items.push({
      to: '/student/growth',
      label: '我的成长',
      shortLabel: '成长',
      showUnreadDot: hasUnreadPraise.value,
    })
    items.push({
      to: '/student/feedback',
      label: '意见反馈',
      shortLabel: '反馈',
      showUnreadDot: unreadSummary.value.has_unread,
    })
  }

  if (auth.user?.role !== 'student') {
    items.push(
      { to: '/admin', label: '统计看板', shortLabel: '看板' },
      { to: '/admin/knowledge', label: '知识库', shortLabel: '知识' },
    )
  }

  if (auth.user?.role === 'admin') {
    items.push(
      { to: '/admin/feedback', label: '意见反馈', shortLabel: '反馈' },
      { to: '/admin/conversations', label: '会话归档', shortLabel: '归档' },
      { to: '/admin/audit', label: '审计日志', shortLabel: '审计' },
      { to: '/admin/agent', label: '智能体配置', shortLabel: '智能体' },
      { to: '/admin/roles', label: '教学角色', shortLabel: '角色' },
      { to: '/admin/models', label: '模型管理', shortLabel: '模型' },
      { to: '/admin/system-config', label: '系统参数', shortLabel: '参数' },
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

watch(() => auth.user?.role, (role) => {
  if (window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === null) {
    sidebarCollapsed.value = role === 'student'
  }
})

watch(
  () => [auth.initialized, auth.user?.role, route.fullPath] as const,
  () => {
    if (!auth.initialized || auth.user?.role !== 'student') {
      unreadSummary.value = {
        unread_feedback_replies: 0,
        unread_release_notes: 0,
        has_unread: false,
      }
      hasUnreadPraise.value = false
      return
    }
    fetchFeedbackUnreadSummary()
      .then((summary) => {
        unreadSummary.value = summary
      })
      .catch(() => {
        unreadSummary.value = {
          unread_feedback_replies: 0,
          unread_release_notes: 0,
          has_unread: false,
        }
      })
    fetchIncentiveSummary()
      .then((summary) => {
        hasUnreadPraise.value = summary.has_unread_praise
      })
      .catch(() => {
        hasUnreadPraise.value = false
      })
  },
  { immediate: true },
)

async function handleLogout() {
  if (!window.confirm('确定退出登录吗？')) {
    return
  }
  const redirectedToSso = await auth.logout()
  if (!redirectedToSso) {
    router.push('/login')
  }
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, sidebarCollapsed.value ? '1' : '0')
}
</script>

<template>
  <div
    :class="[
      'app-root',
      {
        'app-root--shell': shellActive,
        'app-root--plain': !shellActive,
        'app-root--sidebar-collapsed': shellActive && sidebarIsCollapsed,
        'app-root--student-chat': shellActive && studentChatActive,
      },
    ]"
  >
    <div class="ambient ambient-one"></div>
    <div class="ambient ambient-two"></div>
    <aside v-if="shellActive" :class="['app-sidebar', { 'app-sidebar--collapsed': sidebarIsCollapsed }]">
      <div class="sidebar-body">
        <div class="sidebar-head">
          <div class="sidebar-brand">
            <a class="toolbox-link" href="http://10.50.159.62/platform/" aria-label="于博的工具箱，返回功能选择目录页"><span>于博的</span><span>工具箱</span></a>
            <p class="eyebrow" role="img" aria-label="StudyAgent">
              <span class="sidebar-brand-name" aria-hidden="true">StudyAgent</span>
              <span class="sidebar-brand-mark" aria-hidden="true">SA</span>
            </p>
            <p class="brand-title">{{ sidebarIsCollapsed ? '答疑' : '高中学科答疑' }}</p>
            <p v-if="!sidebarIsCollapsed" class="brand-copy">专属知识库 + 苏格拉底引导，先帮你想清楚，再帮你做出来。</p>
          </div>
          <button class="sidebar-toggle" :aria-expanded="!sidebarIsCollapsed" aria-label="展开或收起侧边栏" @click="toggleSidebar">
            {{ sidebarIsCollapsed ? '展开' : '收起' }}
          </button>
        </div>
        <nav class="nav-list">
          <template v-if="studentChatActive">
            <button type="button" title="对话历史" aria-label="对话历史" @click="chatView?.openHistory()">{{ sidebarIsCollapsed ? '历史' : '对话历史' }}</button>
            <button type="button" title="新问题" @click="chatView?.newQuestion()">新问题</button>
            <button type="button" title="学校通知" @click="chatView?.openNotices()">{{ sidebarIsCollapsed ? '通知' : '学校通知' }}</button>
          </template>
          <RouterLink
            v-for="item in navigationItems"
            :key="item.to"
            :to="item.to"
            :title="item.label"
            :aria-label="item.label"
          >
            <span class="nav-link__short">{{ item.shortLabel }}</span>
            <span v-if="!sidebarIsCollapsed" class="nav-link__label">{{ item.label }}</span>
            <span v-if="item.showUnreadDot" class="nav-unread-dot" aria-label="有未读内容"></span>
          </RouterLink>

        </nav>
      </div>
      <div class="sidebar-footer">
        <div class="profile-chip">
          <strong>{{ sidebarIsCollapsed ? sidebarUserBadge : auth.user.full_name }}</strong>
          <span v-if="!sidebarIsCollapsed">{{ auth.user.role }}</span>
        </div>
        <button type="button" class="ghost-button sidebar-logout" aria-label="退出登录" @click="handleLogout">
          {{ sidebarIsCollapsed ? '退出' : '退出登录' }}
        </button>
      </div>
    </aside>
    <main
      :class="[
        'app-main',
        {
          'app-main--plain': !shellActive,
          'app-main--student-chat': shellActive && studentChatActive,
        },
      ]"
    >
      <div v-if="!routeReady" class="auth-guard">
        <p class="panel-subcopy">正在检查登录状态...</p>
      </div>
      <RouterView v-else v-slot="{ Component }"><component :is="Component" ref="chatView" /></RouterView>
    </main>
  </div>
</template>
