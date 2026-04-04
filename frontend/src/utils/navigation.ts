export function forceLoginRedirect() {
  if (typeof window === 'undefined') {
    return
  }
  if (window.location.pathname !== '/login') {
    window.location.replace('/login')
  }
}
