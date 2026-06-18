export interface OidcLoginConfig {
  enabled: boolean
  message?: string
}

export function shouldShowOidcLogin(config: OidcLoginConfig | null | undefined): boolean {
  return config?.enabled === true
}
