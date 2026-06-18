import { api } from './api'
import { shouldShowOidcLogin, type OidcLoginConfig } from './oidcConfig'

export async function fetchOidcLoginEnabled(): Promise<boolean> {
  try {
    const { data } = await api.get<OidcLoginConfig>('/auth/oidc/config', { skipAuthRedirect: true })
    return shouldShowOidcLogin(data)
  } catch {
    return false
  }
}
