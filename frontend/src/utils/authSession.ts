export const ACCESS_TOKEN_STORAGE_KEY = 'studyagent-access-token'
export const REFRESH_TOKEN_STORAGE_KEY = 'studyagent-refresh-token'
export const SSO_SESSION_STORAGE_KEY = 'studyagent-sso-session'
export const SESSION_EXPIRED_EVENT = 'studyagent:session-expired'

// Keycloak 统一认证登出地址（未带 id_token_hint 时 Keycloak 会先弹确认页，属预期）
export const SSO_LOGOUT_URL =
  'http://192.168.1.206/auth/realms/school-platform/protocol/openid-connect/logout' +
  '?client_id=studyagent' +
  '&post_logout_redirect_uri=' +
  encodeURIComponent('http://192.168.1.206/studyagent/')

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

// SSO 登录的用户退出时联动登出 Keycloak；返回 true 表示已触发跳转，调用方不应再做本地路由跳转
export function redirectToSsoLogoutIfNeeded(): boolean {
  if (!storageAvailable()) {
    return false
  }
  if (!window.localStorage.getItem(SSO_SESSION_STORAGE_KEY)) {
    return false
  }
  window.localStorage.removeItem(SSO_SESSION_STORAGE_KEY)
  window.location.href = SSO_LOGOUT_URL
  return true
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
