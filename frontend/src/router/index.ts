import { createRouter, createWebHistory } from 'vue-router'

import pinia from '../pinia'
import { useAuthStore } from '../stores/auth'
import { forceLoginRedirect } from '../utils/navigation'

const Login = () => import('../views/Login.vue')
const StudentChat = () => import('../views/StudentChat.vue')
const AdminDashboard = () => import('../views/AdminDashboard.vue')
const KnowledgeManage = () => import('../views/KnowledgeManage.vue')
const AuditLogs = () => import('../views/AuditLogs.vue')
const AgentConfig = () => import('../views/AgentConfig.vue')
const UserManage = () => import('../views/UserManage.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/student' },
    { path: '/login', component: Login, meta: { public: true } },
    { path: '/student', component: StudentChat, meta: { requiresAuth: true, roles: ['student'] } },
    { path: '/admin', component: AdminDashboard, meta: { requiresAuth: true, roles: ['admin', 'teacher'] } },
    { path: '/admin/knowledge', component: KnowledgeManage, meta: { requiresAuth: true, roles: ['admin', 'teacher'] } },
    { path: '/admin/audit', component: AuditLogs, meta: { requiresAuth: true, roles: ['admin'] } },
    { path: '/admin/agent', component: AgentConfig, meta: { requiresAuth: true, roles: ['admin'] } },
    { path: '/admin/users', component: UserManage, meta: { requiresAuth: true, roles: ['admin'] } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore(pinia)
  const isAuthenticated = await auth.ensureSessionReady()

  if (to.meta.public) {
    if (to.path === '/login' && isAuthenticated && auth.user) {
      return auth.user.role === 'student' ? '/student' : '/admin'
    }
    return true
  }

  if (to.meta.requiresAuth && !isAuthenticated) {
    forceLoginRedirect()
    return false
  }
  const roles = to.meta.roles as string[] | undefined
  if (roles && auth.user && !roles.includes(auth.user.role)) {
    return auth.user.role === 'student' ? '/student' : '/admin'
  }
  return true
})

export default router
