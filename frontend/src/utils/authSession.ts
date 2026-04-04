export const ACCESS_TOKEN_STORAGE_KEY = 'studyagent-access-token'
export const REFRESH_TOKEN_STORAGE_KEY = 'studyagent-refresh-token'
export const SESSION_EXPIRED_EVENT = 'studyagent:session-expired'

let sessionExpiredNotified = false

function storageAvailable() {
  return typeof window !== 'undefined' && typeof window.localStorage !== 'undefined'
}

export function getStoredAccessToken(): string {
  if (!storageAvailable()) {
    return ''
  }
  return window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY) || ''
}

export function getStoredRefreshToken(): string {
  if (!storageAvailable()) {
    return ''
  }
  return window.localStorage.getItem(REFRESH_TOKEN_STORAGE_KEY) || ''
}

export function storeAuthTokens(accessToken: string, refreshToken: string) {
  if (!storageAvailable()) {
    return
  }
  sessionExpiredNotified = false
  window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, accessToken)
  window.localStorage.setItem(REFRESH_TOKEN_STORAGE_KEY, refreshToken)
}

export function clearStoredAuthTokens() {
  if (!storageAvailable()) {
    return
  }
  window.localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY)
  window.localStorage.removeItem(REFRESH_TOKEN_STORAGE_KEY)
}

export function resetSessionExpiredState() {
  sessionExpiredNotified = false
}

export function notifySessionExpired(message = '登录已过期，请重新登录') {
  clearStoredAuthTokens()
  if (typeof window === 'undefined' || sessionExpiredNotified) {
    return
  }
  sessionExpiredNotified = true
  window.dispatchEvent(new CustomEvent<{ message: string }>(SESSION_EXPIRED_EVENT, {
    detail: { message },
  }))
}
