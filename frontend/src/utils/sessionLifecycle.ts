import { ElMessage } from 'element-plus'
import type { Pinia } from 'pinia'
import type { Router } from 'vue-router'

import { useAuthStore } from '../stores/auth'
import { SESSION_EXPIRED_EVENT } from './authSession'
import { forceLoginRedirect } from './navigation'

let installed = false

export function installSessionLifecycle(pinia: Pinia, router: Router) {
  if (installed || typeof window === 'undefined') {
    return
  }

  installed = true
  window.addEventListener(SESSION_EXPIRED_EVENT, async (event: Event) => {
    const auth = useAuthStore(pinia)
    auth.clearSession()
    const detail = (event as CustomEvent<{ message?: string }>).detail
    const message = typeof detail?.message === 'string' && detail.message.trim()
      ? detail.message
      : '登录已过期，请重新登录'

    if (router.currentRoute.value.path !== '/login') {
      await router.replace('/login')
      if (router.currentRoute.value.path !== '/login') {
        forceLoginRedirect()
        return
      }
    }

    ElMessage.warning(message)
  })
}
