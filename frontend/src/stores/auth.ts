import { defineStore } from 'pinia'

import { api } from '../utils/api'
import {
  clearStoredAuthTokens,
  getStoredAccessToken,
  getStoredRefreshToken,
  redirectToSsoLogoutIfNeeded,
  resetSessionExpiredState,
} from '../utils/authSession'

export interface UserInfo {
  id: number
  username: string
  full_name: string
  role: 'student' | 'teacher' | 'admin'
  must_change_password: boolean
}

let sessionReadyPromise: Promise<boolean> | null = null

export const useAuthStore = defineStore('auth', {
  state: () => ({
    accessToken: getStoredAccessToken(),
    refreshToken: getStoredRefreshToken(),
    user: null as UserInfo | null,
    initialized: false,
  }),
  actions: {
    clearSession() {
      this.accessToken = ''
      this.refreshToken = ''
      this.user = null
      this.initialized = true
      clearStoredAuthTokens()
    },
    async ensureSessionReady() {
      if (this.initialized) {
        return Boolean(this.accessToken && this.user)
      }
      if (sessionReadyPromise) {
        return sessionReadyPromise
      }
      sessionReadyPromise = (async () => {
        if (!this.accessToken) {
          this.user = null
          this.initialized = true
          return false
        }
        try {
          await this.fetchProfile({ skipAuthRedirect: true })
          return true
        } catch {
          this.clearSession()
          return false
        } finally {
          this.initialized = true
          sessionReadyPromise = null
        }
      })()
      return sessionReadyPromise
    },
    async fetchProfile(options: { skipAuthRedirect?: boolean } = {}) {
      if (!this.accessToken) {
        this.user = null
        return
      }
      const { data } = await api.get<UserInfo>('/auth/me', {
        skipAuthRedirect: options.skipAuthRedirect,
      })
      this.user = data
    },
    async changePassword(currentPassword: string, newPassword: string) {
      await api.post('/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
      })
      this.clearSession()
      resetSessionExpiredState()
    },
    async logout(): Promise<boolean> {
      try {
        const accessToken = getStoredAccessToken()
        const refreshToken = getStoredRefreshToken()
        if (refreshToken && accessToken) {
          await api.post('/auth/logout', { refresh_token: refreshToken }, { skipAuthRedirect: true })
        }
      } catch {
        // The session may already be invalidated on the server.
      } finally {
        this.clearSession()
        resetSessionExpiredState()
      }
      // SSO 登录的用户联动登出 Keycloak；返回 true 表示已触发整页跳转
      return redirectToSsoLogoutIfNeeded()
    },
  },
})
