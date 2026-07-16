<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { useAuthorizedAssets } from '../composables/useAuthorizedAssets'
import { useAuthStore } from '../stores/auth'
import {
  api,
  type AgentRolePublic,
  type ChatModelOption,
  type ChatModelStatus,
  type ChatSubjectOption,
  type ChatConversationRead,
  type ChatMessageAttachment,
  type ChatMessageRead,
  fetchActiveNotifications,
  fetchChatModelStatuses,
  fetchChatModels,
  fetchChatSubjects,
  fetchIncentiveSummary,
  fetchQuestionRecommendations,
  streamChat,
  type KnowledgeAsset,
  type IncentiveGrant,
  type IncentiveSummary,
  type NotificationItem,
  type QuestionRecommendation,
} from '../utils/api'
import { forceLoginRedirect } from '../utils/navigation'
import {
  createCroppedImageFile,
  previewRectToNaturalRect,
  type CropRect,
} from '../utils/imageCrop'
import {
  CHAT_IMAGE_TOO_LARGE_MESSAGE,
  HEIC_EXPORT_MESSAGE,
  isSupportedChatImageFile,
  prepareChatImageUpload,
} from '../utils/chatImageUpload'
import { collectInlineAssetIds, renderRichText, type InlineRichTextAsset } from '../utils/richText'

interface ConversationSummary {
  id: number
  subject: string
  topic: string
  guidance_stage: string
  resolved: boolean
}

type RecommendationMode = 'context' | 'keyword'

const SHOW_RECOMMENDATION_PANEL = false
const RECOMMENDATION_FETCH_LIMIT = 3
const RECOMMENDATION_PAGE_SIZE = 3
const recommendationDifficultyOptions = [
  { value: 'basic', label: '简单优先' },
  { value: 'standard', label: '标准题' },
  { value: 'advanced', label: '更难题' },
] as const
const GUIDANCE_STAGE_LABELS: Record<string, string> = {
  initial_guidance: '先梳理题意',
  scaffold_hint: '正在搭起解题思路',
  fallback_walkthrough: '等你完成最后一步',
}
const STARTER_PROMPTS = [
  '我已经读完题目，但还不知道第一步该做什么。',
  '我试过一种方法，卡在这里了：',
  '请先帮我检查已知条件，不要直接给答案。',
]
const DEFAULT_SUBJECT_NAMES = ['语文', '数学', '英语', '物理', '化学', '生物', '政治', '历史', '地理']
const DEFAULT_CHAT_SUBJECTS: ChatSubjectOption[] = DEFAULT_SUBJECT_NAMES.map((name) => ({
  name,
  knowledge_base_available: false,
  question_bank_available: false,
}))
const DEFAULT_CHAT_MODELS: ChatModelOption[] = [
  { key: 'deepseek-v4-flash', name: 'DeepSeek V4 Flash', description: '通用快捷' },
]
const CHAT_DRAFT_STORAGE_PREFIX = 'studyagent-student-chat-draft:v1'
const CHAT_DRAFT_POINTER_PREFIX = 'studyagent-student-chat-active-draft:v1'
const SAFE_IMAGE_ERROR_MESSAGES = new Set([
  '只支持上传 1 张图片',
  CHAT_IMAGE_TOO_LARGE_MESSAGE,
  HEIC_EXPORT_MESSAGE,
  '当前浏览器不支持图片裁剪',
  '图片裁剪失败，请重试',
])
const resourceTypeOptions = [
  { value: 'knowledge_note', label: '知识讲义' },
  { value: 'textbook', label: '教材' },
  { value: 'exercise', label: '习题例题' },
  { value: 'question_set', label: '题库试卷' },
  { value: 'extension', label: '拓展资料' },
]
const difficultyOptions = [
  { value: 'basic', label: '基础' },
  { value: 'standard', label: '标准' },
  { value: 'advanced', label: '提高' },
  { value: 'challenge', label: '挑战' },
]
const IMAGE_ONLY_PLACEHOLDER = '[图片提问]'
const MODEL_STATUS_REFRESH_MS = 300000
const form = reactive({
  subject: '物理',
  message: '',
  llmModel: 'deepseek-v4-flash',
  roleId: null as number | null,
})
const passwordForm = reactive({
  currentPassword: '',
  newPassword: '',
  confirmPassword: '',
})
const conversations = ref<ConversationSummary[]>([])
const messages = ref<ChatMessageRead[]>([])
const notifications = ref<NotificationItem[]>([])
const incentiveSummary = ref<IncentiveSummary>({
  total_points: 0,
  level: 1,
  next_level_points: null,
  current_streak_days: 0,
  longest_streak_days: 0,
  badges: [],
  counters: {},
  has_unread_praise: false,
})
const currentConversationId = ref<number | null>(null)
const sending = ref(false)
const historyOpen = ref(false)
const settingsOpen = ref(false)
const resolveDialogVisible = ref(false)
const resolveSubmitting = ref(false)
const resolveReflection = ref('')
const currentConversationResolved = ref(false)
const passwordDialogVisible = ref(false)
const passwordChanging = ref(false)
const deletingConversationIds = ref<Set<number>>(new Set())
const guidanceStage = ref('initial_guidance')
const recommendationPool = ref<QuestionRecommendation[]>([])
const recommendationOffset = ref(0)
const recommendationSeed = ref('')
const recommendationSeedMode = ref<RecommendationMode | ''>('')
const recommendationLoading = ref(false)
const recommendationError = ref('')
const recommendationDifficulty = ref<'basic' | 'standard' | 'advanced'>('basic')
const recommendationMode = ref<RecommendationMode>('context')
const recommendationKeyword = ref('')
const chatStreamRef = ref<HTMLElement | null>(null)
const messageInputRef = ref<{ $el?: HTMLElement } | null>(null)
const cameraInputRef = ref<HTMLInputElement | null>(null)
const galleryInputRef = ref<HTMLInputElement | null>(null)
const cropImageRef = ref<HTMLImageElement | null>(null)
const cropStageRef = ref<HTMLElement | null>(null)
const pendingImageFile = ref<File | null>(null)
const pendingImagePreviewUrl = ref('')
const cropDialogVisible = ref(false)
const cropSourceFile = ref<File | null>(null)
const cropSourceUrl = ref('')
const cropSelection = reactive<CropRect>({ x: 0, y: 0, width: 1, height: 1 })
const cropDragging = ref(false)
const cropApplying = ref(false)
const previousSubject = ref(form.subject)
const chatSubjects = ref<ChatSubjectOption[]>(DEFAULT_CHAT_SUBJECTS)
const chatModels = ref<ChatModelOption[]>(DEFAULT_CHAT_MODELS)
const agentRoles = ref<AgentRolePublic[]>([])
const chatModelStatuses = ref<Record<string, ChatModelStatus>>({})
const modelStatusLoading = ref(false)
const screenReaderAnnouncement = ref('')
const activeDraftStorageKey = ref('')
let hydratingDraft = false
let streamAbortController: AbortController | null = null
let modelStatusTimer: ReturnType<typeof window.setInterval> | null = null
let stopRequested = false
let cropPointerStart: {
  pointerId: number
  mode: 'draw' | 'move' | 'resize'
  x: number
  y: number
  initial: CropRect
} | null = null
const localAttachmentUrls = new Set<string>()
const { assetUrl, openAsset, preloadAssets } = useAuthorizedAssets()
const authStore = useAuthStore()

const visibleRecommendations = computed(() => (
  recommendationPool.value.slice(recommendationOffset.value, recommendationOffset.value + RECOMMENDATION_PAGE_SIZE)
))

const selectedModelStatus = computed(() => chatModelStatuses.value[form.llmModel]?.status || 'unknown')
const selectedModel = computed(() => chatModels.value.find((item) => item.key === form.llmModel) || null)
const selectedModelQuotaExhausted = computed(() => Boolean(selectedModel.value?.quota?.quota_exhausted))
const selectedSubject = computed(() => (
  chatSubjects.value.find((item) => item.name === form.subject)
  || DEFAULT_CHAT_SUBJECTS.find((item) => item.name === form.subject)
  || null
))
const selectedSubjectCapabilityLabel = computed(() => subjectCapabilityLabel(selectedSubject.value))
const studentMessageCount = computed(() => messages.value.filter((item) => item.role === 'user').length)
const showCompletionAction = computed(() => Boolean(
  currentConversationId.value
  && (currentConversationResolved.value || studentMessageCount.value >= 2),
))
const serviceStatusLabel = computed(() => {
  if (selectedModelQuotaExhausted.value || selectedModelStatus.value === 'unavailable') {
    return '正在切换可用服务'
  }
  if (selectedModelStatus.value === 'available') {
    return '答疑服务可用'
  }
  return modelStatusLoading.value ? '正在检测答疑服务' : '答疑服务状态待确认'
})
const sessionStageLabel = computed(() => (
  currentConversationResolved.value ? '已完成本次思考' : stageLabel(guidanceStage.value)
))
const resolveReflectionLength = computed(() => resolveReflection.value.trim().length)
const canSkipReflection = computed(() => resolveReflectionLength.value === 0)
const canSubmitReflection = computed(() => (
  resolveReflectionLength.value >= 20 && resolveReflectionLength.value <= 500
))
const resolveReflectionHint = computed(() => {
  if (resolveReflectionLength.value === 0) {
    return '可以直接跳过；如果写下关键思路，请至少填写 20 字。'
  }
  if (resolveReflectionLength.value < 20) {
    return `还差 ${20 - resolveReflectionLength.value} 字，或清空后选择跳过反思。`
  }
  return '这段总结会帮助你以后回看自己的思路。'
})
const canSend = computed(() => (
  Boolean(form.message.trim() || pendingImageFile.value)
  && selectedModelStatus.value !== 'unavailable'
  && !selectedModelQuotaExhausted.value
))
const hasRecommendations = computed(() => visibleRecommendations.value.length > 0)
const notificationText = computed(() => {
  if (!notifications.value.length) {
    return '暂无通知'
  }
  return notifications.value.map((item) => `${item.title}：${item.content}`).join('   /   ')
})
const notificationTitle = computed(() => notifications.value[0]?.title || '学校通知')
const canRequestRecommendations = computed(() => {
  if (recommendationMode.value === 'keyword') {
    return recommendationKeyword.value.trim().length >= 2
  }
  return Boolean(currentConversationId.value && getLastUserMessage())
})

function subjectCapabilityLabel(subject: ChatSubjectOption | null): string {
  if (subject?.question_bank_available) {
    return '校本资料与题库可用'
  }
  if (subject?.knowledge_base_available) {
    return '校本资料可用'
  }
  return '通用答疑'
}

function draftStorageAvailable(): boolean {
  return typeof window !== 'undefined' && typeof window.sessionStorage !== 'undefined'
}

function readDraftStorageValue(storageKey: string): string {
  if (!storageKey || !draftStorageAvailable()) {
    return ''
  }
  try {
    return window.sessionStorage.getItem(storageKey) || ''
  } catch {
    return ''
  }
}

function writeDraftStorageValue(storageKey: string, value: string): boolean {
  if (!storageKey || !draftStorageAvailable()) {
    return false
  }
  try {
    window.sessionStorage.setItem(storageKey, value)
    return true
  } catch {
    return false
  }
}

function removeDraftStorageValue(storageKey: string) {
  if (!storageKey || !draftStorageAvailable()) {
    return
  }
  try {
    window.sessionStorage.removeItem(storageKey)
  } catch {
    // Storage may be unavailable in a restricted WebView; drafting must still work in memory.
  }
}

function buildDraftStorageKey(
  subject = form.subject,
  conversationId: number | null = currentConversationId.value,
): string {
  const studentKey = authStore.user?.id ?? authStore.user?.username ?? 'student'
  const conversationKey = conversationId ? `conversation-${conversationId}` : 'new'
  return `${CHAT_DRAFT_STORAGE_PREFIX}:${studentKey}:${subject}:${conversationKey}`
}

function buildDraftPointerKey(): string {
  const studentKey = authStore.user?.id ?? authStore.user?.username ?? 'student'
  return `${CHAT_DRAFT_POINTER_PREFIX}:${studentKey}`
}

function readDraftPointer(): {
  storage_key: string
  subject: string
  conversation_id: number | null
} | null {
  if (!draftStorageAvailable()) {
    return null
  }
  try {
    const raw = readDraftStorageValue(buildDraftPointerKey())
    if (!raw) {
      return null
    }
    const parsed = JSON.parse(raw) as Record<string, unknown>
    if (typeof parsed.storage_key !== 'string' || typeof parsed.subject !== 'string') {
      removeDraftStorageValue(buildDraftPointerKey())
      return null
    }
    return {
      storage_key: parsed.storage_key,
      subject: parsed.subject,
      conversation_id: typeof parsed.conversation_id === 'number' ? parsed.conversation_id : null,
    }
  } catch {
    removeDraftStorageValue(buildDraftPointerKey())
    return null
  }
}

function writeDraftPointer(storageKey: string) {
  if (!draftStorageAvailable()) {
    return
  }
  writeDraftStorageValue(buildDraftPointerKey(), JSON.stringify({
    storage_key: storageKey,
    subject: form.subject,
    conversation_id: currentConversationId.value,
  }))
}

function removeDraft(storageKey: string) {
  if (!storageKey || !draftStorageAvailable()) {
    return
  }
  removeDraftStorageValue(storageKey)
  if (readDraftPointer()?.storage_key === storageKey) {
    removeDraftStorageValue(buildDraftPointerKey())
  }
}

function persistDraft(value: string) {
  if (hydratingDraft || !draftStorageAvailable()) {
    return
  }
  const storageKey = activeDraftStorageKey.value || buildDraftStorageKey()
  activeDraftStorageKey.value = storageKey
  if (value.trim()) {
    if (writeDraftStorageValue(storageKey, value)) {
      writeDraftPointer(storageKey)
    }
  } else {
    removeDraft(storageKey)
  }
}

function setDraftInput(value: string) {
  hydratingDraft = true
  form.message = value
  hydratingDraft = false
}

function loadDraftForCurrentContext() {
  const storageKey = buildDraftStorageKey()
  activeDraftStorageKey.value = storageKey
  const storedDraft = readDraftStorageValue(storageKey)
  setDraftInput(storedDraft)
  if (storedDraft.trim()) {
    writeDraftPointer(storageKey)
  }
}

async function restoreLatestDraftContext(): Promise<boolean> {
  const pointer = readDraftPointer()
  if (!pointer || !draftStorageAvailable()) {
    return false
  }
  const draft = readDraftStorageValue(pointer.storage_key)
  if (!draft.trim()) {
    removeDraft(pointer.storage_key)
    return false
  }
  if (pointer.conversation_id && conversations.value.some((item) => item.id === pointer.conversation_id)) {
    return openConversation(pointer.conversation_id)
  }

  const subject = chatSubjects.value.some((item) => item.name === pointer.subject) ? pointer.subject : form.subject
  currentConversationId.value = null
  form.subject = subject
  previousSubject.value = subject
  const nextStorageKey = buildDraftStorageKey()
  if (pointer.storage_key !== nextStorageKey) {
    removeDraft(pointer.storage_key)
    writeDraftStorageValue(nextStorageKey, draft)
  }
  activeDraftStorageKey.value = nextStorageKey
  setDraftInput(draft)
  writeDraftPointer(nextStorageKey)
  return true
}

function clearSubmittedDraft(storageKey: string) {
  removeDraft(storageKey)
  activeDraftStorageKey.value = buildDraftStorageKey()
  removeDraft(activeDraftStorageKey.value)
  setDraftInput('')
}

function restoreSubmittedDraft(storageKey: string, value: string) {
  const currentStorageKey = buildDraftStorageKey()
  if (storageKey !== currentStorageKey) {
    removeDraft(storageKey)
  }
  activeDraftStorageKey.value = currentStorageKey
  setDraftInput(value)
  persistDraft(value)
}

function requestStatus(error: unknown): number | null {
  const status = (
    error as {
      status?: unknown
      response?: { status?: unknown }
    }
  )?.status ?? (error as { response?: { status?: unknown } })?.response?.status
  return typeof status === 'number' ? status : null
}

function requestErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message.trim() : ''
}

function isOffline(): boolean {
  return typeof navigator !== 'undefined' && navigator.onLine === false
}

function safeImageFailureMessage(error: unknown, fallback: string): string {
  const message = requestErrorMessage(error)
  return SAFE_IMAGE_ERROR_MESSAGES.has(message) ? message : fallback
}

watch(() => form.message, (value) => {
  persistDraft(value)
}, { flush: 'sync' })

function scrollToBottom() {
  if (!chatStreamRef.value) {
    return
  }
  chatStreamRef.value.scrollTop = chatStreamRef.value.scrollHeight
}

function queueScrollToBottom() {
  void nextTick(() => {
    scrollToBottom()
  })
}

function handleMessageInputFocus() {
  window.setTimeout(() => {
    const root = messageInputRef.value?.$el || document.querySelector<HTMLElement>('.chat-message-input')
    root?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, 300)
}

async function loadConversations() {
  const { data } = await api.get<ConversationSummary[]>('/chat/history')
  conversations.value = data
  if (currentConversationId.value) {
    currentConversationResolved.value = Boolean(
      data.find((item) => item.id === currentConversationId.value)?.resolved,
    )
  }
}

async function loadNotifications() {
  try {
    notifications.value = await fetchActiveNotifications()
  } catch (error) {
    console.error(error)
    notifications.value = []
  }
}

async function loadChatSubjects() {
  try {
    const subjects = await fetchChatSubjects()
    chatSubjects.value = subjects.length ? subjects : DEFAULT_CHAT_SUBJECTS
  } catch (error) {
    console.error(error)
    chatSubjects.value = DEFAULT_CHAT_SUBJECTS
  }
}

async function loadChatModels() {
  try {
    const models = await fetchChatModels()
    chatModels.value = models.length ? models : DEFAULT_CHAT_MODELS
    if (!chatModels.value.some((item) => item.key === form.llmModel)) {
      form.llmModel = chatModels.value[0]?.key || 'deepseek-v4-flash'
    }
    selectBestAvailableModel()
  } catch {
    chatModels.value = DEFAULT_CHAT_MODELS
    selectBestAvailableModel()
  }
}

async function loadAgentRoles(subject = form.subject) {
  try {
    const { data } = await api.get<AgentRolePublic[]>('/agent-roles/enabled', { params: { subject } })
    agentRoles.value = data
    if (form.roleId !== null && !data.some((item) => item.id === form.roleId)) {
      form.roleId = null
    }
  } catch (error) {
    console.error(error)
    agentRoles.value = []
    form.roleId = null
  }
}

async function refreshChatModelStatuses() {
  modelStatusLoading.value = true
  try {
    const statuses = await fetchChatModelStatuses()
    chatModelStatuses.value = Object.fromEntries(statuses.map((item) => [item.key, item]))
    selectBestAvailableModel()
  } catch {
    chatModelStatuses.value = {}
  } finally {
    modelStatusLoading.value = false
  }
}

function chatModelStatus(modelKey: string): ChatModelStatus {
  return chatModelStatuses.value[modelKey] || {
    key: modelKey,
    status: modelStatusLoading.value ? 'unknown' : 'unknown',
    message: modelStatusLoading.value ? '检测中' : '状态未知',
  }
}

function isChatModelUnavailable(modelKey: string): boolean {
  const model = chatModels.value.find((item) => item.key === modelKey)
  return chatModelStatus(modelKey).status === 'unavailable' || Boolean(model?.quota?.quota_exhausted)
}

function selectBestAvailableModel() {
  const current = chatModels.value.find((item) => item.key === form.llmModel)
  if (current && !isChatModelUnavailable(current.key)) {
    return
  }
  const next = chatModels.value.find((item) => !isChatModelUnavailable(item.key))
  if (next) {
    form.llmModel = next.key
  }
}

async function openConversation(id: number): Promise<boolean> {
  try {
    const { data } = await api.get<ChatConversationRead>(`/chat/history/${id}`)
    currentConversationId.value = id
    resetPendingImage()
    clearLocalAttachmentUrls()
    form.subject = data.subject
    form.roleId = null
    previousSubject.value = data.subject
    guidanceStage.value = data.guidance_stage
    currentConversationResolved.value = data.resolved
    messages.value = data.messages.map((item) => ({
      role: item.role,
      content: item.content,
      attachment: item.attachment || null,
      assets: item.assets || [],
      suggested_replies: normalizeSuggestedReplies(item.suggested_replies),
      agent_role_snapshot: item.agent_role_snapshot || null,
    }))
    loadDraftForCurrentContext()
    await loadAgentRoles(data.subject)
    await preloadMessageAttachments(messages.value)
    resetRecommendations()
    historyOpen.value = false
    queueScrollToBottom()
    return true
  } catch (error) {
    console.error(error)
    ElMessage.error('这条学习记录暂时无法打开，请刷新记录后重试。')
    return false
  }
}

function resetResolveDialog() {
  if (resolveSubmitting.value) {
    return
  }
  resolveDialogVisible.value = false
  resolveReflection.value = ''
}

function openResolveDialog() {
  if (!currentConversationId.value || currentConversationResolved.value || sending.value) {
    return
  }
  resolveReflection.value = ''
  resolveDialogVisible.value = true
}

async function completeConversation(skipReflection: boolean) {
  if (!currentConversationId.value || resolveSubmitting.value) {
    return
  }
  if (skipReflection ? !canSkipReflection.value : !canSubmitReflection.value) {
    return
  }
  const reflection = skipReflection ? null : resolveReflection.value.trim()
  resolveSubmitting.value = true
  try {
    const { data } = await api.post<ChatConversationRead>(`/chat/${currentConversationId.value}/resolve`, {
      resolved: true,
      reflection,
    })
    currentConversationResolved.value = data.resolved
    resolveReflection.value = ''
    resolveDialogVisible.value = false
    if (data.incentive?.points_awarded) {
      ElMessage.success(`完成思考，成长积分 +${data.incentive.points_awarded}`)
    } else {
      ElMessage.success('已完成本次思考')
    }
    await loadIncentiveSummary()
    try {
      await loadConversations()
    } catch (refreshError) {
      console.error(refreshError)
      ElMessage.warning('完成状态已保存，但学习记录暂未刷新。稍后打开历史记录时可再试。')
    }
  } catch (error) {
    console.error(error)
    ElMessage.error('暂时无法完成本次思考，请稍后重试。你的反思内容仍保留在这里。')
  } finally {
    resolveSubmitting.value = false
  }
}

async function restoreConversation() {
  if (!currentConversationId.value || !currentConversationResolved.value || sending.value || resolveSubmitting.value) {
    return
  }
  resolveSubmitting.value = true
  try {
    const { data } = await api.post<ChatConversationRead>(`/chat/${currentConversationId.value}/resolve`, {
      resolved: false,
      reflection: null,
    })
    currentConversationResolved.value = data.resolved
    ElMessage.success('已恢复为继续思考')
    try {
      await loadConversations()
    } catch (refreshError) {
      console.error(refreshError)
      ElMessage.warning('恢复状态已保存，但学习记录暂未刷新。稍后打开历史记录时可再试。')
    }
  } catch (error) {
    console.error(error)
    ElMessage.error('暂时无法恢复这次思考，请稍后重试。')
  } finally {
    resolveSubmitting.value = false
  }
}

async function loadIncentiveSummary() {
  try {
    incentiveSummary.value = await fetchIncentiveSummary()
  } catch {
    // 激励默认关闭或临时不可用时不影响答疑主链路。
  }
}

function startNewConversation(options: { subject?: string } = {}) {
  currentConversationId.value = null
  currentConversationResolved.value = false
  messages.value = []
  guidanceStage.value = 'initial_guidance'
  if (options.subject) {
    form.subject = options.subject
    previousSubject.value = options.subject
  }
  resetPendingImage()
  clearLocalAttachmentUrls()
  resetRecommendations()
  historyOpen.value = false
  loadDraftForCurrentContext()
  queueScrollToBottom()
}

function applyStarterPrompt(prompt: string) {
  form.message = prompt
  void nextTick(() => {
    const root = messageInputRef.value?.$el || document.querySelector<HTMLElement>('.chat-message-input')
    root?.querySelector<HTMLTextAreaElement>('textarea')?.focus()
  })
}

function openPasswordDialog() {
  passwordForm.currentPassword = ''
  passwordForm.newPassword = ''
  passwordForm.confirmPassword = ''
  passwordDialogVisible.value = true
}

function handleSessionMenuCommand(command: string) {
  if (command === 'settings') {
    settingsOpen.value = !settingsOpen.value
    return
  }
  if (command === 'resolve' && showCompletionAction.value) {
    if (currentConversationResolved.value) {
      void restoreConversation()
    } else {
      openResolveDialog()
    }
    return
  }
  if (command === 'password') {
    openPasswordDialog()
  }
}

async function submitPasswordChange() {
  if (!passwordForm.currentPassword || !passwordForm.newPassword || !passwordForm.confirmPassword) {
    ElMessage.info('请完整填写当前密码和新密码')
    return
  }
  if (passwordForm.newPassword.length < 6) {
    ElMessage.error('新密码至少需要 6 位')
    return
  }
  if (passwordForm.newPassword !== passwordForm.confirmPassword) {
    ElMessage.error('两次输入的新密码不一致')
    return
  }
  passwordChanging.value = true
  try {
    await authStore.changePassword(passwordForm.currentPassword, passwordForm.newPassword)
    ElMessage.success('密码已修改，请重新登录')
    passwordDialogVisible.value = false
    forceLoginRedirect()
  } catch (error) {
    const detail = (
      error as {
        response?: {
          data?: {
            detail?: string
          }
        }
      }
    )?.response?.data?.detail
    ElMessage.error(typeof detail === 'string' && detail ? detail : '密码修改失败，请检查当前密码')
  } finally {
    passwordChanging.value = false
  }
}

async function deleteConversation(item: ConversationSummary) {
  if (sending.value || deletingConversationIds.value.has(item.id)) {
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认删除“${conversationTopic(item)}”吗？删除后该对话记录将不再显示。`,
      '删除对话',
      {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return
  }

  deletingConversationIds.value = new Set(deletingConversationIds.value).add(item.id)
  try {
    await api.delete(`/chat/${item.id}`)
    removeDraft(buildDraftStorageKey(item.subject, item.id))
    if (currentConversationId.value === item.id) {
      startNewConversation({ subject: form.subject })
    }
    await loadConversations()
    ElMessage.success('对话已删除')
  } catch {
    ElMessage.error('对话删除失败，请稍后重试')
  } finally {
    const next = new Set(deletingConversationIds.value)
    next.delete(item.id)
    deletingConversationIds.value = next
  }
}

async function handleSubjectChange(nextSubject: string) {
  const oldSubject = previousSubject.value
  if (!currentConversationId.value || messages.value.length === 0) {
    previousSubject.value = nextSubject
    form.roleId = null
    await loadAgentRoles(nextSubject)
    loadDraftForCurrentContext()
    return
  }

  try {
    await ElMessageBox.confirm(
      '切换学科建议新建对话，避免不同学科上下文混在一起。',
      '新建对话',
      {
        confirmButtonText: '新建对话',
        cancelButtonText: '留在当前对话',
        type: 'warning',
      },
    )
    startNewConversation({ subject: nextSubject })
    form.roleId = null
    await loadAgentRoles(nextSubject)
  } catch {
    form.subject = oldSubject
    await loadAgentRoles(oldSubject)
    loadDraftForCurrentContext()
  }
}

function stopStreaming(showNotice = true) {
  stopRequested = true
  streamAbortController?.abort()
  streamAbortController = null
  screenReaderAnnouncement.value = '已停止本次生成，可以继续修改问题。'
  if (showNotice) {
    ElMessage.info('已停止本次生成')
  }
}

function getLastUserMessage(): string {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const item = messages.value[index]
    const content = item.content.trim()
    if (item.role === 'user' && content && content !== IMAGE_ONLY_PLACEHOLDER) {
      return item.content.trim()
    }
  }
  return ''
}

function isLocalPreviewUrl(url?: string | null): boolean {
  return typeof url === 'string' && (url.startsWith('blob:') || url.startsWith('data:'))
}

function trackLocalAttachmentUrl(url?: string | null) {
  if (url && isLocalPreviewUrl(url)) {
    localAttachmentUrls.add(url)
  }
}

function revokeLocalAttachmentUrl(url?: string | null) {
  if (!url || !isLocalPreviewUrl(url)) {
    return
  }
  URL.revokeObjectURL(url)
  localAttachmentUrls.delete(url)
}

function clearLocalAttachmentUrls() {
  localAttachmentUrls.forEach((url) => {
    URL.revokeObjectURL(url)
  })
  localAttachmentUrls.clear()
}

function resetPendingImage(options: { preservePreview?: boolean } = {}) {
  const currentPreviewUrl = pendingImagePreviewUrl.value
  if (options.preservePreview) {
    trackLocalAttachmentUrl(currentPreviewUrl)
  } else {
    revokeLocalAttachmentUrl(currentPreviewUrl)
  }
  pendingImageFile.value = null
  pendingImagePreviewUrl.value = ''
  if (cameraInputRef.value) {
    cameraInputRef.value.value = ''
  }
  if (galleryInputRef.value) {
    galleryInputRef.value.value = ''
  }
}

function resetCropDialog() {
  revokeLocalAttachmentUrl(cropSourceUrl.value)
  cropDialogVisible.value = false
  cropSourceFile.value = null
  cropSourceUrl.value = ''
  cropDragging.value = false
  cropApplying.value = false
  cropPointerStart = null
}

function updatePendingImage(file: File) {
  resetPendingImage()
  pendingImageFile.value = file
  pendingImagePreviewUrl.value = URL.createObjectURL(file)
}

function triggerCameraCapture() {
  if (!sending.value) {
    cameraInputRef.value?.click()
  }
}

function triggerGalleryPicker() {
  if (!sending.value) {
    galleryInputRef.value?.click()
  }
}

async function handleImageSelection(event: Event) {
  const input = event.target as HTMLInputElement | null
  const file = input?.files?.[0]
  if (!file) {
    return
  }
  if (!isSupportedChatImageFile(file)) {
    resetPendingImage()
    ElMessage.error('只支持上传 1 张图片')
    return
  }
  try {
    const prepared = await prepareChatImageUpload(file)
    updatePendingImage(prepared.file)
    prepared.qualityWarnings.forEach((qualityWarning) => {
      ElMessage.warning(qualityWarning)
    })
  } catch (error) {
    resetPendingImage()
    console.error(error)
    ElMessage.error(safeImageFailureMessage(error, '图片处理失败，请重试'))
  }
}

function removePendingImage() {
  resetPendingImage()
}

function openCropDialog(file: File) {
  resetCropDialog()
  cropSourceFile.value = file
  cropSourceUrl.value = URL.createObjectURL(file)
  cropDialogVisible.value = true
}

function initializeCropSelection() {
  const image = cropImageRef.value
  if (!image) {
    return
  }
  const width = image.clientWidth
  const height = image.clientHeight
  cropSelection.width = Math.max(1, Math.round(width))
  cropSelection.height = Math.max(1, Math.round(height))
  cropSelection.x = 0
  cropSelection.y = 0
}

function cropSelectionStyle() {
  return {
    left: `${cropSelection.x}px`,
    top: `${cropSelection.y}px`,
    width: `${cropSelection.width}px`,
    height: `${cropSelection.height}px`,
  }
}

function cropPointerPosition(event: PointerEvent) {
  const image = cropImageRef.value
  if (!image) {
    return { x: 0, y: 0 }
  }
  const bounds = image.getBoundingClientRect()
  return {
    x: Math.max(0, Math.min(event.clientX - bounds.left, bounds.width)),
    y: Math.max(0, Math.min(event.clientY - bounds.top, bounds.height)),
  }
}

function clampPreviewCrop(rect: CropRect): CropRect {
  const image = cropImageRef.value
  if (!image) {
    return rect
  }
  const x = Math.max(0, Math.min(rect.x, image.clientWidth - 1))
  const y = Math.max(0, Math.min(rect.y, image.clientHeight - 1))
  return {
    x,
    y,
    width: Math.max(1, Math.min(rect.width, image.clientWidth - x)),
    height: Math.max(1, Math.min(rect.height, image.clientHeight - y)),
  }
}

function setCropSelection(rect: CropRect) {
  const next = clampPreviewCrop(rect)
  cropSelection.x = Math.round(next.x)
  cropSelection.y = Math.round(next.y)
  cropSelection.width = Math.round(next.width)
  cropSelection.height = Math.round(next.height)
}

function startCropDraw(event: PointerEvent) {
  if (cropApplying.value) {
    return
  }
  const point = cropPointerPosition(event)
  cropPointerStart = {
    pointerId: event.pointerId,
    mode: 'draw',
    x: point.x,
    y: point.y,
    initial: { ...cropSelection },
  }
  cropDragging.value = true
  setCropSelection({ x: point.x, y: point.y, width: 1, height: 1 })
  cropStageRef.value?.setPointerCapture(event.pointerId)
}

function startCropMove(event: PointerEvent) {
  event.stopPropagation()
  const point = cropPointerPosition(event)
  cropPointerStart = {
    pointerId: event.pointerId,
    mode: 'move',
    x: point.x,
    y: point.y,
    initial: { ...cropSelection },
  }
  cropDragging.value = true
  cropStageRef.value?.setPointerCapture(event.pointerId)
}

function startCropResize(event: PointerEvent) {
  event.stopPropagation()
  const point = cropPointerPosition(event)
  cropPointerStart = {
    pointerId: event.pointerId,
    mode: 'resize',
    x: point.x,
    y: point.y,
    initial: { ...cropSelection },
  }
  cropDragging.value = true
  cropStageRef.value?.setPointerCapture(event.pointerId)
}

function updateCropSelection(event: PointerEvent) {
  if (!cropPointerStart) {
    return
  }
  const point = cropPointerPosition(event)
  const start = cropPointerStart
  if (start.mode === 'draw') {
    const x = Math.min(start.x, point.x)
    const y = Math.min(start.y, point.y)
    setCropSelection({
      x,
      y,
      width: Math.abs(point.x - start.x),
      height: Math.abs(point.y - start.y),
    })
    return
  }
  if (start.mode === 'move') {
    const image = cropImageRef.value
    const maxX = image ? Math.max(0, image.clientWidth - start.initial.width) : start.initial.x
    const maxY = image ? Math.max(0, image.clientHeight - start.initial.height) : start.initial.y
    setCropSelection({
      x: Math.max(0, Math.min(start.initial.x + point.x - start.x, maxX)),
      y: Math.max(0, Math.min(start.initial.y + point.y - start.y, maxY)),
      width: start.initial.width,
      height: start.initial.height,
    })
    return
  }
  setCropSelection({
    x: start.initial.x,
    y: start.initial.y,
    width: start.initial.width + point.x - start.x,
    height: start.initial.height + point.y - start.y,
  })
}

function endCropInteraction(event: PointerEvent) {
  if (!cropPointerStart) {
    return
  }
  cropStageRef.value?.releasePointerCapture(cropPointerStart.pointerId)
  cropPointerStart = null
  cropDragging.value = false
  if (cropSelection.width < 12 || cropSelection.height < 12) {
    initializeCropSelection()
  }
  event.stopPropagation()
}

function useOriginalCropSource() {
  if (!cropSourceFile.value) {
    return
  }
  updatePendingImage(cropSourceFile.value)
  resetCropDialog()
}

async function applyImageCrop() {
  const file = cropSourceFile.value
  const image = cropImageRef.value
  if (!file || !image) {
    return
  }
  cropApplying.value = true
  try {
    const naturalRect = previewRectToNaturalRect(
      { ...cropSelection },
      { width: image.clientWidth, height: image.clientHeight },
      { width: image.naturalWidth, height: image.naturalHeight },
    )
    const croppedFile = await createCroppedImageFile(file, image, naturalRect)
    updatePendingImage(croppedFile)
    resetCropDialog()
    ElMessage.success('已裁剪图片，将上传选中区域')
  } catch (error) {
    console.error(error)
    ElMessage.error(safeImageFailureMessage(error, '图片裁剪失败，请重试'))
    cropApplying.value = false
  }
}

function messageAttachmentSrc(attachment?: ChatMessageAttachment | null): string {
  if (!attachment?.url) {
    return ''
  }
  return isLocalPreviewUrl(attachment.url) ? attachment.url : assetUrl(attachment)
}

async function preloadMessageAttachments(items: ChatMessageRead[]) {
  const attachments = items.flatMap((item) => {
    if (!item.attachment?.content_type.startsWith('image/') || isLocalPreviewUrl(item.attachment.url)) {
      return []
    }
    return [item.attachment]
  })
  const messageAssets = items.flatMap((item) => item.assets || [])
  const assets = [...attachments, ...messageAssets]
  if (assets.length) {
    await preloadAssets(assets)
  }
}

function openMessageAttachment(attachment?: ChatMessageAttachment | null) {
  if (!attachment?.url) {
    return
  }
  if (isLocalPreviewUrl(attachment.url)) {
    window.open(attachment.url, '_blank', 'noopener,noreferrer')
    return
  }
  void openAsset(attachment).catch((error) => {
    console.error(error)
    ElMessage.error('图片打开失败，请稍后重试')
  })
}

function resetRecommendations() {
  recommendationPool.value = []
  recommendationOffset.value = 0
  recommendationSeed.value = ''
  recommendationSeedMode.value = ''
  recommendationError.value = ''
}

function resourceTypeLabel(value: string) {
  return resourceTypeOptions.find((item) => item.value === value)?.label || value
}

function stageLabel(value: string) {
  return GUIDANCE_STAGE_LABELS[value] || value
}

function conversationTopic(item: ConversationSummary) {
  return item.topic?.trim() || `${item.subject}答疑`
}

function difficultyLabel(value?: string | null) {
  if (!value) {
    return ''
  }
  return difficultyOptions.find((item) => item.value === value)?.label || value
}

function gradeLabel(value?: number | null) {
  if (!value) {
    return ''
  }
  const labels: Record<number, string> = {
    1: '高一',
    2: '高二',
    3: '高三',
  }
  return labels[value] || `${value}年级`
}

function recommendationMeta(item: QuestionRecommendation): string[] {
  const meta: string[] = [resourceTypeLabel(item.resource_type)]
  if (item.grade) {
    meta.push(gradeLabel(item.grade))
  }
  if (item.chapter) {
    meta.push(item.chapter)
  }
  if (item.section) {
    meta.push(item.section)
  }
  if (item.difficulty) {
    meta.push(`难度 ${difficultyLabel(item.difficulty)}`)
  }
  return meta
}

function buildInlineAssets(assets: KnowledgeAsset[]): InlineRichTextAsset[] {
  return assets.map((asset) => ({
    asset,
    src: asset.content_type.startsWith('image/') ? assetUrl(asset) : '',
  }))
}

function renderMessageBody(content: string, assets: KnowledgeAsset[] = []): string {
  return renderRichText(content, { inlineAssets: buildInlineAssets(assets) })
}

function imageAssets(item: QuestionRecommendation): KnowledgeAsset[] {
  const inlineAssetIds = collectInlineAssetIds(item.question_text, item.assets)
  return item.assets.filter((asset) => asset.content_type.startsWith('image/') && !inlineAssetIds.has(asset.asset_id))
}

function otherAssets(item: QuestionRecommendation): KnowledgeAsset[] {
  return item.assets.filter((asset) => !asset.content_type.startsWith('image/'))
}

function recommendationTitle(item: QuestionRecommendation): string {
  if (item.question_number) {
    return `第${item.question_number}题`
  }
  return '推荐练习'
}

function recommendationDetail(error: unknown): string {
  if (isOffline()) {
    return '网络连接已断开，恢复网络后可重新获取练习题'
  }
  const status = requestStatus(error)
  if (status === 429) {
    return '练习推荐请求较多，请稍后再试'
  }
  if (status && status >= 500) {
    return '练习推荐服务暂时繁忙，请稍后再试'
  }
  return '推荐题获取失败，请稍后重试'
}

function recommendationModeLabel(mode: RecommendationMode): string {
  return mode === 'context' ? '当前问答上下文' : '学生关键词'
}

function currentRecommendationContextLabel(): string {
  const latestUserMessage = getLastUserMessage()
  if (latestUserMessage) {
    return latestUserMessage
  }
  const currentConversation = conversations.value.find((item) => item.id === currentConversationId.value)
  if (currentConversation) {
    return conversationTopic(currentConversation)
  }
  return '当前问答上下文'
}

function chatFailureMessage(error: unknown): string {
  const message = requestErrorMessage(error)
  if (message === 'Password change required') {
    return '请先通过“更多”中的“修改密码”完成改密，再继续提问。草稿已保留。'
  }
  if (message === 'Question is not a supported academic prompt') {
    return '当前只支持学科相关问题，请换一个和学习内容相关的问题再试。草稿已保留。'
  }
  if (isOffline()) {
    return '网络连接已断开，恢复网络后可以重新发送。草稿已保留。'
  }
  const status = requestStatus(error)
  if (status === 429) {
    return '当前提问较多，请稍等片刻再发送。草稿已保留。'
  }
  if (status === 403) {
    return '当前账号暂时无法发送问题，请联系老师或管理员。草稿已保留。'
  }
  if (status && status >= 500) {
    return '答疑服务暂时繁忙，请稍后重新发送。草稿已保留。'
  }
  if (message === 'SSE stream interrupted before completion') {
    return '回复连接中断，请重新发送或稍后再试。草稿已保留。'
  }
  return '暂时无法发送问题，请稍后重试。草稿已保留。'
}

async function requestRecommendations(options: { silent?: boolean } = {}) {
  recommendationLoading.value = true
  recommendationError.value = ''
  recommendationOffset.value = 0
  try {
    const payload = recommendationMode.value === 'context'
      ? {
          subject: form.subject,
          recommendation_mode: 'context' as const,
          conversation_id: currentConversationId.value,
          limit: RECOMMENDATION_FETCH_LIMIT,
          difficulty_preference: recommendationDifficulty.value,
        }
      : {
          subject: form.subject,
          recommendation_mode: 'keyword' as const,
          question: recommendationKeyword.value.trim(),
          limit: RECOMMENDATION_FETCH_LIMIT,
          difficulty_preference: recommendationDifficulty.value,
        }

    if (recommendationMode.value === 'context' && !currentConversationId.value) {
      if (!options.silent) {
        ElMessage.info('请先发送至少一条消息，再按当前问答上下文推荐题目')
      }
      recommendationLoading.value = false
      return
    }
    if (recommendationMode.value === 'keyword' && recommendationKeyword.value.trim().length < 2) {
      if (!options.silent) {
        ElMessage.info('请输入至少 2 个字的关键词')
      }
      recommendationLoading.value = false
      return
    }

    recommendationSeed.value = recommendationMode.value === 'context'
      ? currentRecommendationContextLabel()
      : recommendationKeyword.value.trim()
    recommendationSeedMode.value = recommendationMode.value

    const data = await fetchQuestionRecommendations(payload)
    recommendationPool.value = data
    await preloadAssets(data.flatMap((item) => item.assets))
    if (!data.length) {
      recommendationError.value = '暂时没有匹配到可推荐的练习题'
      if (!options.silent) {
        ElMessage.info(recommendationError.value)
      }
    }
  } catch (error) {
    recommendationPool.value = []
    recommendationError.value = recommendationDetail(error)
    if (!options.silent) {
      ElMessage.error(recommendationError.value)
    }
  } finally {
    recommendationLoading.value = false
  }
}

function refreshRecommendations() {
  void requestRecommendations()
}

function switchRecommendationDifficulty(nextDifficulty: 'basic' | 'standard' | 'advanced') {
  if (recommendationDifficulty.value === nextDifficulty || recommendationLoading.value) {
    return
  }
  recommendationDifficulty.value = nextDifficulty
  if (recommendationSeedMode.value) {
    void requestRecommendations({ silent: true })
  }
}

function changeRecommendationMode(nextMode: RecommendationMode) {
  if (recommendationMode.value === nextMode) {
    return
  }
  recommendationMode.value = nextMode
  resetRecommendations()
}

function rotateRecommendations() {
  if (recommendationLoading.value) {
    return
  }
  const nextOffset = recommendationOffset.value + RECOMMENDATION_PAGE_SIZE
  if (nextOffset < recommendationPool.value.length) {
    recommendationOffset.value = nextOffset
    return
  }
  if (recommendationSeedMode.value) {
    void requestRecommendations({ silent: true })
  }
}

function applyRecommendationToInput(item: QuestionRecommendation) {
  const prefix = item.contains_images
    ? '请围绕下面这道题继续引导我。注意：题图我会自己看，你先基于题干文字帮助我梳理思路：'
    : '请围绕下面这道题继续引导我，不要直接给答案：'
  form.message = `${prefix}\n${item.question_text}`
  ElMessage.success('题目已带入输入框，可继续追问')
}

function normalizeSuggestedReplies(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return []
  }
  const replies: string[] = []
  const seen = new Set<string>()
  value.forEach((item) => {
    const reply = String(item || '').trim()
    if (!reply || seen.has(reply)) {
      return
    }
    replies.push(reply)
    seen.add(reply)
  })
  return replies.slice(0, 3)
}

function clearActiveSuggestedReplies() {
  const last = messages.value[messages.value.length - 1]
  if (last?.role === 'assistant') {
    last.suggested_replies = []
  }
}

function canShowSuggestedReplies(index: number, item: ChatMessageRead): boolean {
  return item.role === 'assistant'
    && index === messages.value.length - 1
    && !sending.value
    && normalizeSuggestedReplies(item.suggested_replies).length > 0
}

async function sendSuggestedReply(reply: string) {
  const message = reply.trim()
  if (!message || sending.value) {
    return
  }
  clearActiveSuggestedReplies()
  form.message = message
  await nextTick()
  await sendMessage()
}

async function sendMessage() {
  if (!canSend.value) {
    return
  }
  clearActiveSuggestedReplies()
  const draftText = form.message
  const message = draftText.trim()
  const submissionDraftKey = activeDraftStorageKey.value || buildDraftStorageKey()
  const image = pendingImageFile.value
  const attachment = image ? {
    attachment_id: `local-${Date.now()}`,
    filename: image.name,
    content_type: image.type || 'image/*',
    url: pendingImagePreviewUrl.value,
  } satisfies ChatMessageAttachment : null
  const content = message || (attachment ? IMAGE_ONLY_PLACEHOLDER : '')
  setDraftInput('')
  resetPendingImage({ preservePreview: Boolean(attachment) })
  sending.value = true
  stopRequested = false
  screenReaderAnnouncement.value = '学习助手正在整理回复。'
  streamAbortController = new AbortController()
  messages.value.push({
    role: 'user',
    content,
    attachment,
  })
  messages.value.push({ role: 'assistant', content: '', assets: [], suggested_replies: [] })
  queueScrollToBottom()

  try {
    let roleFallbackNotified = false
    await streamChat(
      {
        subject: form.subject,
        message,
        conversation_id: currentConversationId.value,
        llm_model: form.llmModel,
        role_id: form.roleId,
        image,
      },
      ({ event, data }) => {
        if (event === 'meta') {
          if (typeof data.conversation_id === 'number') {
            currentConversationId.value = data.conversation_id
          }
          if (typeof data.guidance_stage === 'string') {
            guidanceStage.value = data.guidance_stage
          }
          const roleStatus = typeof data.role_status === 'string' ? data.role_status : 'none'
          if (
            form.roleId !== null
            && ['disabled', 'not_found', 'subject_mismatch', 'misconfigured'].includes(roleStatus)
          ) {
            form.roleId = null
            if (!roleFallbackNotified) {
              roleFallbackNotified = true
              ElMessage.warning('所选教学角色当前不可用，已改用默认教学风格')
            }
            void loadAgentRoles()
          }
        }
        if (event === 'restart') {
          const last = messages.value[messages.value.length - 1]
          if (last && last.role === 'assistant') {
            last.content = ''
            last.suggested_replies = []
            queueScrollToBottom()
          }
        }
        if (event === 'chunk') {
          const last = messages.value[messages.value.length - 1]
          if (last && last.role === 'assistant' && typeof data.content === 'string') {
            last.content += last.content ? data.content : data.content.replace(/^\s+/, '')
            queueScrollToBottom()
          }
        }
        if (event === 'done') {
          const last = messages.value[messages.value.length - 1]
          if (last && last.role === 'assistant' && typeof data.content === 'string') {
            if (Array.isArray(data.assets)) {
              last.assets = data.assets as KnowledgeAsset[]
              void preloadAssets(last.assets)
            }
            last.content = data.content
            last.suggested_replies = normalizeSuggestedReplies(data.suggested_replies)
            screenReaderAnnouncement.value = `学习助手回复完成：${data.content}`
            queueScrollToBottom()
          }
        }
        if (event === 'suggested_replies') {
          const last = messages.value[messages.value.length - 1]
          if (last && last.role === 'assistant' && Array.isArray(data.suggested_replies)) {
            last.suggested_replies = normalizeSuggestedReplies(data.suggested_replies)
          }
        }
        if (event === 'incentive') {
          const grant = data as unknown as IncentiveGrant
          if (grant.points_awarded > 0) {
            ElMessage.success(`认真思考已记录，成长积分 +${grant.points_awarded}`)
          }
          if (grant.new_badges?.length) {
            ElMessage.success(`获得新徽章：${grant.new_badges.join('、')}`)
          }
          void loadIncentiveSummary()
        }
      },
      { signal: streamAbortController.signal, retryAttempts: 2, retryDelayMs: 1200 },
    )
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant' && !last.content.trim()) {
      last.content = '这次没有收到有效回复，请重新发送一次，或补充题目条件后再试。'
      screenReaderAnnouncement.value = last.content
      queueScrollToBottom()
    }
    clearSubmittedDraft(submissionDraftKey)
    try {
      await loadConversations()
    } catch (refreshError) {
      console.error(refreshError)
      ElMessage.warning('回复已经完成，但学习记录暂未刷新。稍后打开历史记录时可再试。')
    }
    await loadIncentiveSummary()
    await loadChatModels()
    resetRecommendations()
  } catch (error) {
    const last = messages.value[messages.value.length - 1]
    if (error instanceof DOMException && error.name === 'AbortError') {
      if (stopRequested && last && last.role === 'assistant' && !last.content.trim()) {
        messages.value.pop()
      }
      clearSubmittedDraft(submissionDraftKey)
      return
    }

    console.error(error)
    const failureMessage = chatFailureMessage(error)
    restoreSubmittedDraft(submissionDraftKey, draftText)
    if (image && !pendingImageFile.value) {
      updatePendingImage(image)
    }
    if (last && last.role === 'assistant' && !last.content.trim()) {
      last.content = failureMessage
    }
    screenReaderAnnouncement.value = failureMessage
    ElMessage.error(failureMessage)
  } finally {
    streamAbortController = null
    sending.value = false
    stopRequested = false
  }
}

onBeforeUnmount(() => {
  if (modelStatusTimer) {
    window.clearInterval(modelStatusTimer)
    modelStatusTimer = null
  }
  stopStreaming(false)
  resetResolveDialog()
  resetCropDialog()
  resetPendingImage()
  clearLocalAttachmentUrls()
})

onMounted(async () => {
  void loadNotifications()
  await Promise.all([loadChatSubjects(), loadChatModels()])
  try {
    await loadConversations()
  } catch (error) {
    console.error(error)
    ElMessage.warning('学习记录暂时无法加载，不影响开始新的提问。')
  }
  const restoredDraft = await restoreLatestDraftContext()
  if (!restoredDraft) {
    loadDraftForCurrentContext()
  }
  await loadAgentRoles(form.subject)
  void refreshChatModelStatuses()
  modelStatusTimer = window.setInterval(() => {
    void refreshChatModelStatuses()
  }, MODEL_STATUS_REFRESH_MS)
  await loadIncentiveSummary()
})
</script>

<template>
  <section class="student-page-grid student-study-page">
    <section class="chat-panel study-surface">
      <header class="study-session-header">
        <div class="study-session-heading">
          <div class="study-session-title-row">
            <h1>把问题一步步想清楚</h1>
            <span class="study-stage-chip" :data-complete="currentConversationResolved">{{ sessionStageLabel }}</span>
          </div>
          <p>从你卡住的地方开始，我会用问题陪你找到下一步。</p>
        </div>
        <div class="study-session-tools">
          <div class="session-subject-control">
            <el-select
              v-model="form.subject"
              class="session-subject-select"
              :disabled="sending || resolveSubmitting"
              placeholder="选择学科"
              :aria-label="`当前答疑学科：${form.subject}`"
              aria-describedby="session-subject-capability"
              @change="handleSubjectChange"
            >
              <el-option
                v-for="subject in chatSubjects"
                :key="subject.name"
                :label="subject.name"
                :value="subject.name"
              >
                <div class="subject-option-copy">
                  <strong>{{ subject.name }}</strong>
                  <span>{{ subjectCapabilityLabel(subject) }}</span>
                </div>
              </el-option>
            </el-select>
            <span id="session-subject-capability" class="session-subject-capability">
              {{ selectedSubjectCapabilityLabel }}
            </span>
          </div>
          <button type="button" class="study-tool-button" :disabled="resolveSubmitting" @click="historyOpen = true">
            历史<span v-if="conversations.length" class="study-tool-count">{{ conversations.length }}</span>
          </button>
          <el-dropdown
            :disabled="sending || resolveSubmitting"
            trigger="click"
            placement="bottom-end"
            @command="handleSessionMenuCommand"
          >
            <button
              type="button"
              class="study-tool-button"
              :disabled="sending || resolveSubmitting"
              aria-label="更多会话操作"
              aria-haspopup="menu"
            >
              更多
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="settings">
                  {{ settingsOpen ? '收起引导设置' : '引导设置' }}
                </el-dropdown-item>
                <el-dropdown-item v-if="showCompletionAction" command="resolve" divided>
                  {{ currentConversationResolved ? '恢复继续思考' : '完成本次思考' }}
                </el-dropdown-item>
                <el-dropdown-item command="password">修改密码</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <button
            type="button"
            class="primary-button study-new-question"
            :disabled="sending || resolveSubmitting"
            @click="startNewConversation()"
          >
            新问题
          </button>
        </div>
      </header>

      <section
        v-if="settingsOpen"
        id="study-session-settings"
        class="study-session-settings"
        aria-label="答疑引导设置"
      >
        <div class="study-service-status" role="status" aria-live="polite">
          <span class="study-service-dot" :data-status="selectedModelStatus"></span>
          <div>
            <strong>{{ serviceStatusLabel }}</strong>
            <span>系统会自动选择当前最合适的答疑服务。</span>
          </div>
        </div>
        <label v-if="agentRoles.length" class="study-setting-field">
          <span>引导方式（可选）</span>
          <el-select
            v-model="form.roleId"
            class="chat-role-select"
            :disabled="sending"
            clearable
            placeholder="使用默认引导方式"
            aria-label="选择引导方式"
          >
            <el-option
              v-for="role in agentRoles"
              :key="role.id"
              :value="role.id"
              :label="`${role.emoji || ''}${role.emoji ? ' ' : ''}${role.display_name}`"
            >
              <div class="chat-role-option">
                <strong>{{ role.emoji }} {{ role.display_name }}</strong>
                <span>{{ role.description }}</span>
              </div>
            </el-option>
          </el-select>
        </label>
      </section>

      <details v-if="notifications.length" class="student-notice">
        <summary>
          <span class="student-notice__label">学校通知</span>
          <span class="student-notice__preview">{{ notificationTitle }}</span>
          <span class="student-notice__action">查看</span>
        </summary>
        <p>{{ notificationText }}</p>
      </details>
      <div v-else class="student-notice student-notice--empty" aria-label="学校通知">
        <span class="student-notice__label">学校通知</span>
        <span>暂无新通知，专心解决眼前的问题。</span>
      </div>

      <div
        ref="chatStreamRef"
        class="chat-stream"
        role="log"
        aria-live="off"
        aria-relevant="additions"
        :aria-busy="sending"
      >
        <section v-if="!messages.length" class="chat-empty-state" aria-labelledby="chat-empty-title">
          <div class="chat-empty-copy">
            <h2 id="chat-empty-title">从你已经想到的地方开始</h2>
            <p>可以发题目，也可以先说说你试过什么、具体卡在哪里。我不会直接给最终答案。</p>
          </div>
          <div class="starter-prompts" aria-label="起步示例">
            <button
              v-for="prompt in STARTER_PROMPTS"
              :key="prompt"
              type="button"
              class="starter-prompt"
              @click="applyStarterPrompt(prompt)"
            >
              {{ prompt }}
            </button>
          </div>
        </section>
        <div v-for="(item, index) in messages" :key="index" :class="['message-row', item.role]">
          <article
            :class="['bubble', item.role]"
            :aria-label="item.role === 'user' ? '你的消息' : '学习助手回复'"
          >
            <span class="bubble-role">{{ item.role === 'user' ? '学生' : '导师' }}</span>
            <div
              v-if="item.attachment?.content_type.startsWith('image/')"
              class="recommendation-card__images"
            >
              <a
                class="recommendation-image"
                :href="messageAttachmentSrc(item.attachment) || undefined"
                target="_blank"
                rel="noreferrer"
                @click.prevent="openMessageAttachment(item.attachment)"
              >
                <img
                  v-if="messageAttachmentSrc(item.attachment)"
                  :src="messageAttachmentSrc(item.attachment)"
                  :alt="item.attachment.filename"
                  loading="lazy"
                />
                <span v-else>图片加载中...</span>
                <span>{{ item.attachment.filename }}</span>
              </a>
            </div>
            <div class="message-body" v-html="renderMessageBody(item.content, item.assets || [])"></div>
          </article>
          <div
            v-if="canShowSuggestedReplies(index, item)"
            class="suggested-replies"
            aria-label="可选回复"
          >
            <button
              v-for="reply in normalizeSuggestedReplies(item.suggested_replies)"
              :key="reply"
              type="button"
              class="suggested-reply-chip"
              :disabled="sending"
              @click="sendSuggestedReply(reply)"
            >
              {{ reply }}
            </button>
          </div>
        </div>
      </div>

      <div class="chat-controls">
        <input
          ref="cameraInputRef"
          type="file"
          accept="image/*"
          capture="environment"
          style="display: none"
          @change="handleImageSelection"
        />
        <input
          ref="galleryInputRef"
          type="file"
          accept="image/*"
          style="display: none"
          @change="handleImageSelection"
        />
        <el-input
          ref="messageInputRef"
          v-model="form.message"
          class="chat-message-input"
          :disabled="sending"
          type="textarea"
          :rows="3"
          resize="none"
          placeholder="把题目发给我，也可以先说说你试过什么、卡在哪里"
          aria-label="输入你的问题或当前思路"
          @focus="handleMessageInputFocus"
        />
        <div v-if="pendingImagePreviewUrl" class="composer-attachment-preview">
          <a
            class="recommendation-image"
            :href="pendingImagePreviewUrl"
            target="_blank"
            rel="noreferrer"
            @click.prevent="openMessageAttachment({
              attachment_id: 'composer-image',
              filename: pendingImageFile?.name || '待发送图片',
              content_type: pendingImageFile?.type || 'image/*',
              url: pendingImagePreviewUrl,
            })"
          >
            <img :src="pendingImagePreviewUrl" :alt="pendingImageFile?.name || '待发送图片'" />
            <span>{{ pendingImageFile?.name || '待发送图片' }}</span>
          </a>
          <div class="row-actions composer-attachment-actions">
            <button type="button" class="ghost-button" :disabled="sending" @click="triggerCameraCapture">重新拍照</button>
            <button type="button" class="ghost-button" :disabled="sending" @click="triggerGalleryPicker">从相册替换</button>
            <button type="button" class="ghost-button" :disabled="sending || !pendingImageFile" @click="pendingImageFile && openCropDialog(pendingImageFile)">裁剪</button>
            <button type="button" class="ghost-button ghost-button--danger" :disabled="sending" @click="removePendingImage">移除</button>
          </div>
        </div>
        <p v-if="sending" class="stream-hint">正在陪你梳理思路，可随时停止。</p>
        <div class="chat-composer-footer">
          <div class="chat-secondary-actions">
            <el-dropdown :disabled="sending" trigger="click" placement="top-start">
              <button type="button" class="ghost-button attachment-trigger" :disabled="sending">
                {{ pendingImageFile ? '更换题图' : '添加题图' }}
              </button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="triggerCameraCapture">拍照上传</el-dropdown-item>
                  <el-dropdown-item @click="triggerGalleryPicker">从相册选择</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <span class="composer-helper">一次可上传 1 张题图</span>
          </div>
          <div class="chat-primary-actions">
            <button v-if="sending" type="button" class="ghost-button" @click="stopStreaming()">停止生成</button>
            <button type="button" class="primary-button send-question-button" :disabled="sending || !canSend" @click="sendMessage">
              {{ sending ? '正在思考...' : '发送问题' }}
            </button>
          </div>
        </div>
        <p class="sr-only" aria-live="polite" aria-atomic="true">{{ screenReaderAnnouncement }}</p>
      </div>

      <section v-if="SHOW_RECOMMENDATION_PANEL" class="recommendation-panel">
        <div class="panel-header panel-header--stack">
          <div>
            <p class="eyebrow">Practice Picks</p>
            <h2>推荐练习</h2>
            <p class="panel-subcopy">
              推荐题图片会保留在卡片中；带入聊天时仍只会自动带入题干文字，不会自动进入聊天理解。
            </p>
          </div>
          <div class="recommendation-controls">
            <div class="row-actions">
              <button
                :class="recommendationMode === 'context' ? 'primary-button' : 'ghost-button'"
                :disabled="recommendationLoading"
                @click="changeRecommendationMode('context')"
              >
                按当前问答上下文
              </button>
              <button
                :class="recommendationMode === 'keyword' ? 'primary-button' : 'ghost-button'"
                :disabled="recommendationLoading"
                @click="changeRecommendationMode('keyword')"
              >
                按关键词
              </button>
            </div>
            <el-input
              v-if="recommendationMode === 'keyword'"
              v-model="recommendationKeyword"
              :disabled="recommendationLoading"
              maxlength="500"
              placeholder="输入知识点、题型或章节关键词，例如：牛顿第二定律 受力分析"
            />
            <p v-else class="panel-subcopy">
              将根据当前会话主题与最近提问，从题库里匹配相近题目。
            </p>
          </div>
          <div class="row-actions">
            <div class="row-actions">
              <button
                v-for="item in recommendationDifficultyOptions"
                :key="item.value"
                class="ghost-button"
                :disabled="recommendationLoading || recommendationDifficulty === item.value"
                @click="switchRecommendationDifficulty(item.value)"
              >
                {{ item.label }}
              </button>
            </div>
            <button
              class="primary-button"
              :disabled="recommendationLoading || !canRequestRecommendations"
              @click="refreshRecommendations"
            >
              {{ recommendationLoading ? '推荐中...' : '获取推荐题' }}
            </button>
            <button
              class="ghost-button"
              :disabled="recommendationLoading || !recommendationSeedMode"
              @click="rotateRecommendations"
            >
              换一批
            </button>
          </div>
        </div>

        <p v-if="recommendationSeed" class="recommendation-seed">
          当前推荐依据：{{ recommendationModeLabel(recommendationSeedMode || recommendationMode) }} / {{ recommendationSeed }}
        </p>
        <p v-if="recommendationError" class="recommendation-state recommendation-state--error">
          {{ recommendationError }}
        </p>
        <p v-else-if="recommendationLoading" class="recommendation-state">正在匹配相似练习题...</p>
        <p v-else-if="!hasRecommendations" class="recommendation-state">
          请选择推荐方式后手动获取推荐题；系统不会再自动生成推荐结果。
        </p>

        <div v-if="hasRecommendations" class="recommendation-grid">
          <article
            v-for="item in visibleRecommendations"
            :key="item.chunk_id"
            class="recommendation-card"
          >
            <div class="recommendation-card__head">
              <div>
                <p class="recommendation-card__eyebrow">{{ recommendationTitle(item) }}</p>
                <h3>{{ item.document_filename || '题库资料' }}</h3>
              </div>
              <button class="ghost-button" @click="applyRecommendationToInput(item)">带入输入框</button>
            </div>

            <div class="detail-chip-group">
              <span
                v-for="meta in recommendationMeta(item)"
                :key="`${item.chunk_id}-${meta}`"
                class="detail-chip"
              >
                {{ meta }}
              </span>
              <span v-if="item.contains_images" class="detail-chip">题图 {{ item.image_count }} 张</span>
            </div>

            <div class="recommendation-card__question message-body" v-html="renderMessageBody(item.question_text, item.assets)"></div>

            <div v-if="imageAssets(item).length" class="recommendation-card__images">
              <a
                v-for="asset in imageAssets(item)"
                :key="asset.asset_id"
                class="recommendation-image"
                :href="assetUrl(asset) || undefined"
                target="_blank"
                rel="noreferrer"
                @click.prevent="openAsset(asset)"
              >
                <img v-if="assetUrl(asset)" :src="assetUrl(asset)" :alt="asset.title || asset.filename" loading="lazy" />
                <span v-else>图片加载中...</span>
                <span>{{ asset.title || asset.filename }}</span>
              </a>
            </div>

            <div v-if="otherAssets(item).length" class="recommendation-card__assets">
              <a
                v-for="asset in otherAssets(item)"
                :key="asset.asset_id"
                :href="assetUrl(asset) || undefined"
                target="_blank"
                rel="noreferrer"
                @click.prevent="openAsset(asset)"
              >
                附件：{{ asset.title || asset.filename }}
              </a>
            </div>
          </article>
        </div>
      </section>
    </section>

    <el-drawer
      v-model="historyOpen"
      direction="ltr"
      size="360px"
      :with-header="false"
      class="study-history-drawer"
      aria-label="对话历史"
    >
      <div class="history-drawer-content">
        <header class="history-drawer-header">
          <div>
            <h2>学习记录</h2>
            <p>需要时再回来查看，不打断眼前的思考。</p>
          </div>
          <button type="button" class="study-tool-button" aria-label="关闭对话历史" @click="historyOpen = false">关闭</button>
        </header>
        <div class="history-drawer-actions">
          <button type="button" class="primary-button" :disabled="sending" @click="startNewConversation()">新建对话</button>
          <button type="button" class="ghost-button" @click="loadConversations">刷新</button>
        </div>
        <RouterLink to="/student/growth" class="incentive-summary-card" @click="historyOpen = false">
          <div><span>成长积分</span><strong>{{ incentiveSummary.total_points }}</strong></div>
          <div><span>等级</span><strong>L{{ incentiveSummary.level }}</strong></div>
          <div><span>连续学习</span><strong>{{ incentiveSummary.current_streak_days }} 天</strong></div>
        </RouterLink>
        <div v-if="!conversations.length" class="history-empty-state">
          <strong>还没有学习记录</strong>
          <span>发送第一个问题后，这里会保留你的思考过程。</span>
        </div>
        <div v-else class="conversation-list">
          <article
            v-for="item in conversations"
            :key="item.id"
            class="conversation-card conversation-card--compact"
          >
            <button class="conversation-card__open" :disabled="sending" @click="openConversation(item.id)">
              <strong class="conversation-card__topic">{{ conversationTopic(item) }}</strong>
              <span class="conversation-card__meta">{{ item.subject }} · {{ stageLabel(item.guidance_stage) }}</span>
              <span>{{ item.resolved ? '已解决' : '继续思考' }}</span>
            </button>
            <div class="row-actions conversation-card__actions">
              <button
                type="button"
                class="ghost-button ghost-button--danger"
                :disabled="sending || deletingConversationIds.has(item.id)"
                :aria-label="`${deletingConversationIds.has(item.id) ? '正在删除' : '删除对话'}：${conversationTopic(item)}`"
                @click="deleteConversation(item)"
              >
                {{ deletingConversationIds.has(item.id) ? '删除中...' : '删除' }}
              </button>
            </div>
          </article>
        </div>
      </div>
    </el-drawer>

    <el-dialog
      v-model="resolveDialogVisible"
      class="resolve-dialog"
      title="完成本次思考"
      width="min(92vw, 540px)"
      destroy-on-close
      :close-on-click-modal="false"
      :close-on-press-escape="!resolveSubmitting"
      :show-close="!resolveSubmitting"
      @closed="resetResolveDialog"
    >
      <div class="resolve-dialog__content">
        <p>如果愿意，可以用自己的话写下关键思路；也可以明确跳过反思，不会影响完成本次对话。</p>
        <label for="resolve-reflection">关键思路（可选）</label>
        <el-input
          id="resolve-reflection"
          v-model="resolveReflection"
          type="textarea"
          :rows="5"
          maxlength="500"
          show-word-limit
          resize="none"
          :disabled="resolveSubmitting"
          placeholder="例如：我先确定受力方向，再利用平衡条件判断……"
          aria-label="本次思考的关键思路"
        />
        <p
          class="resolve-dialog__hint"
          :class="{ 'resolve-dialog__hint--warning': resolveReflectionLength > 0 && !canSubmitReflection }"
          aria-live="polite"
        >
          {{ resolveReflectionHint }}
        </p>
      </div>
      <template #footer>
        <div class="resolve-dialog__actions">
          <button type="button" class="ghost-button" :disabled="resolveSubmitting" @click="resetResolveDialog">
            取消
          </button>
          <button
            type="button"
            class="ghost-button"
            :disabled="resolveSubmitting || !canSkipReflection"
            @click="completeConversation(true)"
          >
            跳过反思并完成
          </button>
          <button
            type="button"
            class="primary-button"
            :disabled="resolveSubmitting || !canSubmitReflection"
            @click="completeConversation(false)"
          >
            {{ resolveSubmitting ? '正在保存...' : '提交反思并完成' }}
          </button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="cropDialogVisible"
      class="image-crop-dialog"
      title="裁剪上传区域"
      width="min(92vw, 760px)"
      :close-on-click-modal="false"
      @closed="resetCropDialog"
    >
      <div class="image-cropper">
        <div
          ref="cropStageRef"
          class="image-cropper__stage"
          :class="{ 'image-cropper__stage--dragging': cropDragging }"
          @pointerdown="startCropDraw"
          @pointermove="updateCropSelection"
          @pointerup="endCropInteraction"
          @pointercancel="endCropInteraction"
        >
          <img
            v-if="cropSourceUrl"
            ref="cropImageRef"
            :src="cropSourceUrl"
            alt="待裁剪图片"
            draggable="false"
            @load="initializeCropSelection"
          />
          <div
            v-if="cropSourceUrl"
            class="image-cropper__selection"
            :style="cropSelectionStyle()"
            @pointerdown="startCropMove"
          >
            <span class="image-cropper__handle" @pointerdown="startCropResize"></span>
          </div>
        </div>
        <p class="panel-subcopy">
          拖动图片重新框选题目区域；拖动选框可移动，拖动右下角可调整大小。
        </p>
      </div>
      <template #footer>
        <div class="row-actions">
          <button class="ghost-button" :disabled="cropApplying" @click="resetCropDialog">取消</button>
          <button class="ghost-button" :disabled="cropApplying" @click="useOriginalCropSource">上传原图</button>
          <button class="primary-button" :disabled="cropApplying" @click="applyImageCrop">
            {{ cropApplying ? '裁剪中...' : '使用裁剪区域' }}
          </button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="passwordDialogVisible"
      title="修改密码"
      width="420px"
      destroy-on-close
    >
      <div class="password-dialog">
        <el-input
          v-model="passwordForm.currentPassword"
          type="password"
          show-password
          autocomplete="current-password"
          placeholder="当前密码"
        />
        <el-input
          v-model="passwordForm.newPassword"
          type="password"
          show-password
          autocomplete="new-password"
          placeholder="新密码，至少 6 位"
        />
        <el-input
          v-model="passwordForm.confirmPassword"
          type="password"
          show-password
          autocomplete="new-password"
          placeholder="再次输入新密码"
          @keyup.enter="submitPasswordChange"
        />
        <p class="panel-subcopy">修改成功后，当前账号会退出登录，其他设备上的旧登录态也会失效。</p>
      </div>
      <template #footer>
        <div class="row-actions">
          <button class="ghost-button" :disabled="passwordChanging" @click="passwordDialogVisible = false">取消</button>
          <button class="primary-button" :disabled="passwordChanging" @click="submitPasswordChange">
            {{ passwordChanging ? '修改中...' : '确认修改' }}
          </button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>
