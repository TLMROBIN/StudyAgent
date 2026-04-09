import { defineStore } from 'pinia'

import { api } from '../utils/api'
import {
  clearStoredAuthTokens,
  getStoredAccessToken,
  getStoredRefreshToken,
  resetSessionExpiredState,
  storeAuthTokens,
} from '../utils/authSession'

export interface UserInfo {
  id: number
  username: string
  student_no?: string | null
  full_name: string
  role: 'student' | 'teacher' | 'admin'
  must_change_password: boolean
}

interface LoginPayload {
  access_token: string
  refresh_token: string
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
    async loginStudent(studentNo: string, password: string) {
      const { data } = await api.post<LoginPayload>('/auth/student/login', {
        student_no: studentNo,
        password,
      })
      await this.applyTokenPair(data)
    },
    async loginStaff(username: string, password: string) {
      const { data } = await api.post<LoginPayload>('/auth/staff/login', {
        username,
        password,
      })
      await this.applyTokenPair(data)
    },
    async applyTokenPair(data: LoginPayload) {
      this.accessToken = data.access_token
      this.refreshToken = data.refresh_token
      storeAuthTokens(data.access_token, data.refresh_token)
      this.initialized = false
      await this.fetchProfile()
      this.initialized = true
    },
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
    async logout() {
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
    },
  },
})
