import axios from 'axios'

import {
  getStoredAccessToken,
  getStoredRefreshToken,
  notifySessionExpired,
  storeAuthTokens,
} from './authSession'

declare module 'axios' {
  interface AxiosRequestConfig<D = any> {
    _retryAuthRefresh?: boolean
    skipAuthRedirect?: boolean
  }

  interface InternalAxiosRequestConfig<D = any> {
    _retryAuthRefresh?: boolean
    skipAuthRedirect?: boolean
  }
}

export interface StreamEvent {
  event: string
  data: Record<string, unknown>
}

export interface StreamChatOptions {
  signal?: AbortSignal
  retryAttempts?: number
  retryDelayMs?: number
}

export interface KnowledgeAsset {
  asset_id: string
  filename: string
  content_type: string
  url: string
  title?: string | null
  description?: string | null
}

export interface ChatMessageAttachment {
  attachment_id: string
  filename: string
  content_type: string
  url: string
  size_bytes?: number | null
  width?: number | null
  height?: number | null
}

export interface ChatMessageRead {
  role: 'user' | 'assistant'
  content: string
  attachment?: ChatMessageAttachment | null
}

export interface ChatConversationRead {
  subject: string
  guidance_stage: string
  messages: ChatMessageRead[]
}

export interface StreamChatRequest {
  subject: string
  message?: string
  conversation_id?: number | null
  request_id?: string | null
  llm_model?: string | null
  image?: File | null
}

export interface ChatModelOption {
  key: string
  name: string
  description: string
  billing_mode?: 'request_count' | 'token_usage' | 'free_local'
  quota?: ChatModelQuota
}

export interface ChatModelQuota {
  daily_request_limit?: number | null
  remaining_requests?: number | null
  daily_token_limit?: number | null
  remaining_tokens?: number | null
  quota_exhausted: boolean
  message: string
}

export interface ChatModelStatus {
  key: string
  status: 'available' | 'unavailable' | 'unknown'
  message: string
}

export interface LLMProviderAccount {
  id: number
  provider_name: string
  display_name: string
  base_url: string
  account_billing_type: 'token_plan' | 'pay_as_you_go' | 'local'
  is_enabled: boolean
  has_api_key: boolean
  created_at: string
}

export interface LLMProviderAccountPayload {
  provider_name: string
  display_name: string
  base_url: string
  api_key?: string
  account_billing_type: 'token_plan' | 'pay_as_you_go' | 'local'
  is_enabled?: boolean
}

export interface LLMQuotaPolicyPayload {
  billing_mode: 'request_count' | 'token_usage' | 'free_local'
  user_daily_request_limit?: number | null
  user_daily_token_limit?: number | null
  school_daily_request_limit?: number | null
  school_daily_token_limit?: number | null
  provider_rolling_5h_request_limit?: number | null
  provider_weekly_request_limit?: number | null
  max_completion_tokens?: number | null
  count_cache_hit?: boolean
  fail_closed_on_store_error?: boolean
}

export interface LLMQuotaPolicy extends LLMQuotaPolicyPayload {
  id: number
  count_cache_hit: boolean
  fail_closed_on_store_error: boolean
}

export interface LLMModelConfigPayload {
  model_key: string
  display_name: string
  description?: string
  provider_account_id: number
  provider_model: string
  capability_text?: boolean
  capability_vision?: boolean
  is_enabled?: boolean
  is_primary?: boolean
  is_fallback?: boolean
  sort_order?: number
  quota_policy: LLMQuotaPolicyPayload
}

export interface LLMModelConfig {
  id: number
  model_key: string
  display_name: string
  description: string
  provider_account_id: number
  provider_model: string
  capability_text: boolean
  capability_vision: boolean
  is_enabled: boolean
  is_primary: boolean
  is_fallback: boolean
  sort_order: number
  quota_policy: LLMQuotaPolicy
  created_at: string
}

export interface LLMUsageSummary {
  model_key: string
  billing_mode: string
  request_count: number
  total_tokens: number
}

export type AuthorizedAssetResource = KnowledgeAsset | ChatMessageAttachment

export interface QuestionRecommendationRequest {
  subject: string
  recommendation_mode: 'context' | 'keyword'
  question?: string
  conversation_id?: number | null
  limit?: number
  student_grade?: number | null
  include_solutions?: boolean
  difficulty_preference?: 'basic' | 'standard' | 'advanced'
}

export interface QuestionRecommendation {
  chunk_id: number
  document_id: number
  document_filename?: string | null
  subject: string
  resource_type: string
  grade?: number | null
  chapter?: string | null
  section?: string | null
  difficulty?: string | null
  question_number?: string | null
  question_text: string
  contains_images: boolean
  image_count: number
  assets: KnowledgeAsset[]
  answer_text?: string | null
  explanation_text?: string | null
}

export interface NotificationItem {
  id: number
  title: string
  content: string
  is_archived: boolean
  created_at: string
  updated_at: string
  archived_at?: string | null
}

export interface NotificationPayload {
  title: string
  content: string
}

const rawBase = import.meta.env.VITE_API_BASE_URL || '/api'
export const apiBase = rawBase.endsWith('/') ? rawBase.slice(0, -1) : rawBase

export const api = axios.create({
  baseURL: apiBase,
})

interface TokenRefreshResponse {
  access_token: string
  refresh_token: string
}

let refreshPromise: Promise<string> | null = null

function isAuthBypassRequest(requestUrl: string): boolean {
  return requestUrl.endsWith('/auth/student/login')
    || requestUrl.endsWith('/auth/staff/login')
    || requestUrl.endsWith('/auth/refresh')
}

async function refreshAccessToken(): Promise<string> {
  const refreshToken = getStoredRefreshToken()
  if (!refreshToken) {
    notifySessionExpired()
    throw createSessionExpiredError()
  }

  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const { data } = await axios.post<TokenRefreshResponse>(
          `${apiBase}/auth/refresh`,
          { refresh_token: refreshToken },
          { skipAuthRedirect: true },
        )
        storeAuthTokens(data.access_token, data.refresh_token)
        return data.access_token
      } catch {
        notifySessionExpired()
        throw createSessionExpiredError()
      } finally {
        refreshPromise = null
      }
    })()
  }

  return refreshPromise
}

api.interceptors.request.use((config) => {
  const token = getStoredAccessToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      const requestConfig = error.config
      const requestUrl = requestConfig?.url || ''
      const skipAuthRedirect = requestConfig?.skipAuthRedirect || isAuthBypassRequest(requestUrl)

      if (requestConfig && !requestConfig._retryAuthRefresh && !isAuthBypassRequest(requestUrl)) {
        try {
          const nextAccessToken = await refreshAccessToken()
          requestConfig._retryAuthRefresh = true
          requestConfig.headers = requestConfig.headers ?? {}
          requestConfig.headers.Authorization = `Bearer ${nextAccessToken}`
          return api(requestConfig)
        } catch (refreshError) {
          return Promise.reject(refreshError)
        }
      }

      if (!skipAuthRedirect) {
        notifySessionExpired()
      }
    }
    return Promise.reject(error)
  },
)

export async function fetchQuestionRecommendations(
  payload: QuestionRecommendationRequest,
): Promise<QuestionRecommendation[]> {
  const { data } = await api.post<QuestionRecommendation[]>('/chat/recommendations', payload)
  return data
}

export async function fetchChatModels(): Promise<ChatModelOption[]> {
  const { data } = await api.get<ChatModelOption[]>('/chat/models')
  return data
}

export async function fetchChatModelStatuses(): Promise<ChatModelStatus[]> {
  const { data } = await api.get<ChatModelStatus[]>('/chat/models/status')
  return data
}

export async function fetchActiveNotifications(): Promise<NotificationItem[]> {
  const { data } = await api.get<NotificationItem[]>('/notifications/active')
  return data
}

export async function fetchAdminNotifications(): Promise<NotificationItem[]> {
  const { data } = await api.get<NotificationItem[]>('/admin/notifications')
  return data
}

export async function createAdminNotification(payload: NotificationPayload): Promise<NotificationItem> {
  const { data } = await api.post<NotificationItem>('/admin/notifications', payload)
  return data
}

export async function updateAdminNotification(id: number, payload: NotificationPayload): Promise<NotificationItem> {
  const { data } = await api.put<NotificationItem>(`/admin/notifications/${id}`, payload)
  return data
}

export async function archiveAdminNotification(id: number): Promise<NotificationItem> {
  const { data } = await api.post<NotificationItem>(`/admin/notifications/${id}/archive`)
  return data
}

export async function fetchLLMProviderAccounts(): Promise<LLMProviderAccount[]> {
  const { data } = await api.get<LLMProviderAccount[]>('/llm-providers/accounts')
  return data
}

export async function createLLMProviderAccount(payload: LLMProviderAccountPayload): Promise<LLMProviderAccount> {
  const { data } = await api.post<LLMProviderAccount>('/llm-providers/accounts', payload)
  return data
}

export async function updateLLMProviderAccount(id: number, payload: LLMProviderAccountPayload): Promise<LLMProviderAccount> {
  const { data } = await api.put<LLMProviderAccount>(`/llm-providers/accounts/${id}`, payload)
  return data
}

export async function fetchLLMModelConfigs(): Promise<LLMModelConfig[]> {
  const { data } = await api.get<LLMModelConfig[]>('/llm-providers/models')
  return data
}

export async function createLLMModelConfig(payload: LLMModelConfigPayload): Promise<LLMModelConfig> {
  const { data } = await api.post<LLMModelConfig>('/llm-providers/models', payload)
  return data
}

export async function updateLLMModelConfig(id: number, payload: LLMModelConfigPayload): Promise<LLMModelConfig> {
  const { data } = await api.put<LLMModelConfig>(`/llm-providers/models/${id}`, payload)
  return data
}

export async function deleteLLMModelConfig(id: number): Promise<void> {
  await api.delete(`/llm-providers/models/${id}`)
}

export async function fetchLLMUsageSummary(): Promise<LLMUsageSummary[]> {
  const { data } = await api.get<LLMUsageSummary[]>('/llm-usage/summary')
  return data
}

export function resolveApiUrl(path: string): string {
  if (!path) {
    return path
  }
  if (/^(https?:)?\/\//i.test(path) || path.startsWith('data:') || path.startsWith('blob:')) {
    return path
  }
  const base = /^https?:\/\//i.test(apiBase) ? apiBase : window.location.origin
  try {
    return new URL(path, base).toString()
  } catch {
    return path
  }
}

function extractResponseDetail(payload: string, status: number): string {
  if (payload) {
    try {
      const parsed = JSON.parse(payload) as { detail?: unknown }
      if (typeof parsed.detail === 'string' && parsed.detail.trim()) {
        return parsed.detail
      }
      if (
        parsed.detail
        && typeof parsed.detail === 'object'
        && 'message' in parsed.detail
        && typeof parsed.detail.message === 'string'
        && parsed.detail.message.trim()
      ) {
        return parsed.detail.message
      }
    } catch {
      const trimmedPayload = payload.trim()
      if (status === 502 || status === 503 || status === 504) {
        return '服务暂时不可用，请稍后重试'
      }
      if (/^\s*</.test(trimmedPayload) || /<html[\s>]/i.test(trimmedPayload)) {
        return '请求失败，请稍后重试'
      }
      if (trimmedPayload) {
        return trimmedPayload
      }
    }
  }

  if (status === 502 || status === 503 || status === 504) {
    return '服务暂时不可用，请稍后重试'
  }

  return `Request failed with status ${status}`
}

function createSessionExpiredError(): Error {
  const error = new Error('登录已过期，请重新登录')
  error.name = 'SessionExpiredError'
  return error
}

function isSessionExpiredError(error: unknown): boolean {
  return error instanceof Error && error.name === 'SessionExpiredError'
}

export async function streamChat(
  payload: StreamChatRequest,
  onEvent: (event: StreamEvent) => void,
  options: StreamChatOptions = {},
): Promise<void> {
  const requestId = typeof payload.request_id === 'string' && payload.request_id
    ? payload.request_id
    : (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  const nextPayload: StreamChatRequest = {
    ...payload,
    request_id: requestId,
  }
  const maxAttempts = Math.max(0, options.retryAttempts ?? 2)
  let attempt = 0
  let completed = false

  while (attempt <= maxAttempts) {
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
    let sawDone = false
    try {
      if (attempt > 0) {
        onEvent({ event: 'restart', data: { request_id: requestId, attempt } })
      }

      const response = await fetch(`${apiBase}/chat/stream`, {
        method: 'POST',
        signal: options.signal,
        headers: {
          Authorization: `Bearer ${getStoredAccessToken()}`,
        },
        body: buildStreamChatFormData(nextPayload),
      })

      if (!response.ok || !response.body) {
        if (response.status === 401) {
          try {
            await refreshAccessToken()
            continue
          } catch {
            throw createSessionExpiredError()
          }
        }
        const detail = await response.text().catch(() => '')
        throw new Error(extractResponseDetail(detail, response.status))
      }

      reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) {
          break
        }
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''
        for (const frame of frames) {
          const eventLine = frame.split('\n').find((line) => line.startsWith('event:'))
          const dataLine = frame.split('\n').find((line) => line.startsWith('data:'))
          if (!eventLine || !dataLine) {
            continue
          }
          const event = eventLine.replace('event:', '').trim()
          if (event === 'heartbeat') {
            continue
          }
          const parsedEvent = {
            event,
            data: JSON.parse(dataLine.replace('data:', '').trim()),
          }
          if (event === 'done') {
            sawDone = true
            completed = true
          }
          onEvent(parsedEvent)
        }
      }

      if (sawDone) {
        return
      }
      throw new Error('SSE stream interrupted before completion')
    } catch (error) {
      if (options.signal?.aborted) {
        throw error
      }
      if (isSessionExpiredError(error)) {
        throw error
      }
      if (attempt >= maxAttempts) {
        throw error
      }
      attempt += 1
      await new Promise((resolve) => {
        window.setTimeout(resolve, options.retryDelayMs ?? 800 * attempt)
      })
    } finally {
      if (reader) {
        try {
          await reader.cancel()
        } catch {
          // ignore reader cancellation errors
        }
      }
    }
  }

  if (!completed) {
    throw new Error('SSE stream interrupted before completion')
  }
}

function buildStreamChatFormData(payload: StreamChatRequest): FormData {
  const formData = new FormData()
  formData.append('subject', payload.subject)
  formData.append('message', typeof payload.message === 'string' ? payload.message : '')

  if (typeof payload.conversation_id === 'number') {
    formData.append('conversation_id', String(payload.conversation_id))
  }

  if (typeof payload.request_id === 'string' && payload.request_id) {
    formData.append('request_id', payload.request_id)
  }

  if (typeof payload.llm_model === 'string' && payload.llm_model) {
    formData.append('llm_model', payload.llm_model)
  }

  if (payload.image instanceof File) {
    formData.append('image', payload.image)
  }

  return formData
}
