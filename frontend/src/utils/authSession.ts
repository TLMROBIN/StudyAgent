export const ACCESS_TOKEN_STORAGE_KEY = 'studyagent-access-token'
export const REFRESH_TOKEN_STORAGE_KEY = 'studyagent-refresh-token'
export const ID_TOKEN_HINT_STORAGE_KEY = 'studyagent-id-token-hint'
export const SSO_SESSION_STORAGE_KEY = 'studyagent-sso-session'
export const SESSION_EXPIRED_EVENT = 'studyagent:session-expired'

function buildSsoLogoutUrl(idTokenHint = ''): string {
  const origin = window.location.origin
  const logoutUrl = new URL('/auth/realms/school-platform/protocol/openid-connect/logout', origin)
  logoutUrl.searchParams.set('client_id', 'studyagent')
  if (idTokenHint) {
    logoutUrl.searchParams.set('id_token_hint', idTokenHint)
  }
  logoutUrl.searchParams.set(
    'post_logout_redirect_uri',
    new URL('/directory-admin/api/auth/login', origin).toString(),
  )
  return logoutUrl.toString()
}

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

export function getStoredIdTokenHint(): string {
  if (!storageAvailable()) {
    return ''
  }
  return window.localStorage.getItem(ID_TOKEN_HINT_STORAGE_KEY) || ''
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
  window.localStorage.removeItem(ID_TOKEN_HINT_STORAGE_KEY)
}

export function resetSessionExpiredState() {
  sessionExpiredNotified = false
}

// SSO 登录的用户退出时联动登出 Keycloak；返回 true 表示已触发跳转，调用方不应再做本地路由跳转
export function redirectToSsoLogoutIfNeeded(idTokenHint = ''): boolean {
  if (!storageAvailable()) {
    return false
  }
  if (!window.localStorage.getItem(SSO_SESSION_STORAGE_KEY)) {
    return false
  }
  window.localStorage.removeItem(SSO_SESSION_STORAGE_KEY)
  window.location.href = buildSsoLogoutUrl(idTokenHint)
  return true
}

export function notifySessionExpired(message = '登录已过期，请重新登录') {
  const idTokenHint = getStoredIdTokenHint()
  clearStoredAuthTokens()
  if (typeof window === 'undefined' || sessionExpiredNotified) {
    return
  }
  sessionExpiredNotified = true
  window.dispatchEvent(new CustomEvent<{ message: string; idTokenHint: string }>(SESSION_EXPIRED_EVENT, {
    detail: { message, idTokenHint },
  }))
}
