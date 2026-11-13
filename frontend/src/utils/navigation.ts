// 部署在子路径（WEB_BASE_PATH=/studyagent/）时，登录页必须相对 BASE_URL 解析，
// 否则会跳到根路径 /login，再由 SPA 兜底路由回到 /studyagent/*，形成登录循环。
const loginPath = `${import.meta.env.BASE_URL}login`

export function forceLoginRedirect() {
  if (typeof window === 'undefined') {
    return
  }
  if (window.location.pathname !== loginPath) {
    window.location.replace(loginPath)
  }
}

export function isLoginPath(path: string) {
  return path === '/login' || path === loginPath
}
