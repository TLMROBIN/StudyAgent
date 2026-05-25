<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { useAuthorizedAssets } from '../composables/useAuthorizedAssets'
import { useAuthStore } from '../stores/auth'
import {
  api,
  type ChatModelOption,
  type ChatModelStatus,
  type ChatConversationRead,
  type ChatMessageAttachment,
  type ChatMessageRead,
  fetchChatModelStatuses,
  fetchChatModels,
  fetchQuestionRecommendations,
  streamChat,
  type KnowledgeAsset,
  type QuestionRecommendation,
} from '../utils/api'
import { forceLoginRedirect } from '../utils/navigation'
import {
  createCroppedImageFile,
  previewRectToNaturalRect,
  type CropRect,
} from '../utils/imageCrop'
import { collectInlineAssetIds, renderRichText, type InlineRichTextAsset } from '../utils/richText'

interface ConversationSummary {
  id: number
  subject: string
  topic: string
  guidance_stage: string
  resolved: boolean
}

type RecommendationMode = 'context' | 'keyword'

const RECOMMENDATION_FETCH_LIMIT = 3
const RECOMMENDATION_PAGE_SIZE = 3
const recommendationDifficultyOptions = [
  { value: 'basic', label: '简单优先' },
  { value: 'standard', label: '标准题' },
  { value: 'advanced', label: '更难题' },
] as const
const GUIDANCE_STAGE_LABELS: Record<string, string> = {
  initial_guidance: '初始引导',
  scaffold_hint: '逐步提示',
  fallback_walkthrough: '兜底讲解',
}
const subjects = ['语文', '数学', '英语', '物理', '化学', '生物', '政治', '历史', '地理']
const DEFAULT_CHAT_MODELS: ChatModelOption[] = [
  { key: 'minimax-m27', name: 'MiniMax-M2.7', description: 'highspeed' },
  { key: 'qwen2.5-vl', name: 'qwen2.5-vl', description: '图片理解推荐使用，但响应速度可能较慢。' },
]
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
  subject: '数学',
  message: '',
  llmModel: 'minimax-m27',
})
const passwordForm = reactive({
  currentPassword: '',
  newPassword: '',
  confirmPassword: '',
})
const conversations = ref<ConversationSummary[]>([])
const messages = ref<ChatMessageRead[]>([])
const currentConversationId = ref<number | null>(null)
const sending = ref(false)
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
const chatModels = ref<ChatModelOption[]>(DEFAULT_CHAT_MODELS)
const chatModelStatuses = ref<Record<string, ChatModelStatus>>({})
const modelStatusLoading = ref(false)
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
const canSend = computed(() => (
  Boolean(form.message.trim() || pendingImageFile.value)
  && selectedModelStatus.value !== 'unavailable'
  && !selectedModelQuotaExhausted.value
))
const hasRecommendations = computed(() => visibleRecommendations.value.length > 0)
const guidanceStageLabel = computed(() => stageLabel(guidanceStage.value))
const canRequestRecommendations = computed(() => {
  if (recommendationMode.value === 'keyword') {
    return recommendationKeyword.value.trim().length >= 2
  }
  return Boolean(currentConversationId.value && getLastUserMessage())
})

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

async function loadConversations() {
  const { data } = await api.get<ConversationSummary[]>('/chat/history')
  conversations.value = data
}

async function loadChatModels() {
  try {
    const models = await fetchChatModels()
    chatModels.value = models.length ? models : DEFAULT_CHAT_MODELS
    if (!chatModels.value.some((item) => item.key === form.llmModel)) {
      form.llmModel = chatModels.value[0]?.key || 'minimax-m27'
    }
  } catch {
    chatModels.value = DEFAULT_CHAT_MODELS
  }
}

async function refreshChatModelStatuses() {
  modelStatusLoading.value = true
  try {
    const statuses = await fetchChatModelStatuses()
    chatModelStatuses.value = Object.fromEntries(statuses.map((item) => [item.key, item]))
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

function chatModelStatusLabel(modelKey: string): string {
  const status = chatModelStatus(modelKey)
  const model = chatModels.value.find((item) => item.key === modelKey)
  if (model?.quota?.quota_exhausted) {
    return model.quota.message || '额度已用完'
  }
  if (status.status === 'available') {
    return '可用'
  }
  if (status.status === 'unavailable') {
    return status.message || '不可用'
  }
  return modelStatusLoading.value ? '检测中' : '状态未知'
}

function isChatModelUnavailable(modelKey: string): boolean {
  const model = chatModels.value.find((item) => item.key === modelKey)
  return chatModelStatus(modelKey).status === 'unavailable' || Boolean(model?.quota?.quota_exhausted)
}

function chatModelQuotaLabel(model: ChatModelOption): string {
  const quota = model.quota
  if (!quota) {
    return ''
  }
  if (quota.message) {
    return quota.message
  }
  if (model.billing_mode === 'request_count' && typeof quota.remaining_requests === 'number') {
    return `今日剩余 ${quota.remaining_requests} / ${quota.daily_request_limit ?? '-'} 次`
  }
  if (model.billing_mode === 'token_usage' && typeof quota.remaining_tokens === 'number') {
    return `今日剩余 ${quota.remaining_tokens.toLocaleString()} / ${quota.daily_token_limit ?? '-'} tokens`
  }
  if (model.billing_mode === 'free_local') {
    return '本地模型'
  }
  return ''
}

function selectChatModel(modelKey: string) {
  if (sending.value || isChatModelUnavailable(modelKey)) {
    return
  }
  form.llmModel = modelKey
}

async function openConversation(id: number) {
  currentConversationId.value = id
  resetPendingImage()
  clearLocalAttachmentUrls()
  const { data } = await api.get<ChatConversationRead>(`/chat/history/${id}`)
  form.subject = data.subject
  previousSubject.value = data.subject
  guidanceStage.value = data.guidance_stage
  messages.value = data.messages.map((item) => ({
    role: item.role,
    content: item.content,
    attachment: item.attachment || null,
  }))
  await preloadMessageAttachments(messages.value)
  resetRecommendations()
  queueScrollToBottom()
}

async function toggleResolved() {
  if (!currentConversationId.value) {
    return
  }
  await api.post(`/chat/${currentConversationId.value}/resolve`, { resolved: true })
  await loadConversations()
  ElMessage.success('已标记为已解决')
}

function startNewConversation(options: { subject?: string } = {}) {
  currentConversationId.value = null
  messages.value = []
  guidanceStage.value = 'initial_guidance'
  if (options.subject) {
    form.subject = options.subject
    previousSubject.value = options.subject
  }
  resetPendingImage()
  clearLocalAttachmentUrls()
  resetRecommendations()
  queueScrollToBottom()
}

function openPasswordDialog() {
  passwordForm.currentPassword = ''
  passwordForm.newPassword = ''
  passwordForm.confirmPassword = ''
  passwordDialogVisible.value = true
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
  } catch {
    form.subject = oldSubject
  }
}

function stopStreaming(showNotice = true) {
  stopRequested = true
  streamAbortController?.abort()
  streamAbortController = null
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

function handleImageSelection(event: Event) {
  const input = event.target as HTMLInputElement | null
  const file = input?.files?.[0]
  if (!file) {
    return
  }
  if (!file.type.startsWith('image/')) {
    resetPendingImage()
    ElMessage.error('只支持上传 1 张图片')
    return
  }
  openCropDialog(file)
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
  cropSelection.width = Math.max(1, Math.round(width * 0.82))
  cropSelection.height = Math.max(1, Math.round(height * 0.82))
  cropSelection.x = Math.round((width - cropSelection.width) / 2)
  cropSelection.y = Math.round((height - cropSelection.height) / 2)
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
    ElMessage.error(error instanceof Error ? error.message : '图片裁剪失败，请重试')
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
  if (attachments.length) {
    await preloadAssets(attachments)
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
  const detail = (
    error as {
      response?: {
        data?: {
          detail?: string
        }
      }
    }
  )?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) {
    return detail
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
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
  if (error instanceof Error) {
    const message = error.message.trim()
    if (message === 'Password change required') {
      return '当前账号被标记为需改密，学生问答已被后端拦截，请联系管理员检查账号状态'
    }
    if (message === 'Question is not a supported academic prompt') {
      return '当前只支持学科相关问题，请换一个和学习内容相关的问题再试'
    }
    if (message) {
      return message
    }
  }
  return '发送失败，请稍后重试'
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

async function sendMessage() {
  if (!canSend.value) {
    return
  }
  const message = form.message.trim()
  const image = pendingImageFile.value
  const attachment = image ? {
    attachment_id: `local-${Date.now()}`,
    filename: image.name,
    content_type: image.type || 'image/*',
    url: pendingImagePreviewUrl.value,
  } satisfies ChatMessageAttachment : null
  const content = message || (attachment ? IMAGE_ONLY_PLACEHOLDER : '')
  form.message = ''
  resetPendingImage({ preservePreview: Boolean(attachment) })
  sending.value = true
  stopRequested = false
  streamAbortController = new AbortController()
  messages.value.push({
    role: 'user',
    content,
    attachment,
  })
  messages.value.push({ role: 'assistant', content: '' })
  queueScrollToBottom()

  try {
    await streamChat(
      {
        subject: form.subject,
        message,
        conversation_id: currentConversationId.value,
        llm_model: form.llmModel,
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
        }
        if (event === 'restart') {
          const last = messages.value[messages.value.length - 1]
          if (last && last.role === 'assistant') {
            last.content = ''
            queueScrollToBottom()
          }
        }
        if (event === 'chunk') {
          const last = messages.value[messages.value.length - 1]
          if (last && last.role === 'assistant' && typeof data.content === 'string') {
            last.content += data.content
            queueScrollToBottom()
          }
        }
        if (event === 'done') {
          const last = messages.value[messages.value.length - 1]
          if (last && last.role === 'assistant' && typeof data.content === 'string') {
            last.content = data.content
            queueScrollToBottom()
          }
        }        
      },
      { signal: streamAbortController.signal, retryAttempts: 2, retryDelayMs: 1200 },
    )
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant' && !last.content.trim()) {
      last.content = '这次没有收到有效回复，请重新发送一次，或补充题目条件后再试。'
      queueScrollToBottom()
    }
    await loadConversations()
    await loadChatModels()
    resetRecommendations()
  } catch (error) {
    const last = messages.value[messages.value.length - 1]
    if (error instanceof DOMException && error.name === 'AbortError') {
      if (stopRequested && last && last.role === 'assistant' && !last.content.trim()) {
        messages.value.pop()
      }
      return
    }

    console.error(error)
    const failureMessage = chatFailureMessage(error)
    if (last && last.role === 'assistant' && !last.content.trim()) {
      last.content = failureMessage
    }
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
  resetCropDialog()
  resetPendingImage()
  clearLocalAttachmentUrls()
})

onMounted(async () => {
  await loadChatModels()
  void refreshChatModelStatuses()
  modelStatusTimer = window.setInterval(() => {
    void refreshChatModelStatuses()
  }, MODEL_STATUS_REFRESH_MS)
  await loadConversations()
})
</script>

<template>
  <section class="page-grid student-page-grid">
    <aside class="panel panel-tight student-history-panel">
      <div class="panel-header panel-header--stack">
        <div>
          <p class="eyebrow">Student Workspace</p>
          <h2>对话历史</h2>
        </div>
        <div class="row-actions">
          <button class="primary-button" :disabled="sending" @click="startNewConversation()">新建对话</button>
          <button class="ghost-button" :disabled="sending" @click="openPasswordDialog">修改密码</button>
          <button class="ghost-button" @click="loadConversations">刷新</button>
        </div>
      </div>
      <div class="conversation-list">
        <article
          v-for="item in conversations"
          :key="item.id"
          class="conversation-card conversation-card--compact"
        >
          <button class="conversation-card__open" @click="openConversation(item.id)">
            <strong class="conversation-card__topic">{{ conversationTopic(item) }}</strong>
            <span class="conversation-card__meta">{{ item.subject }}</span>
            <span>{{ stageLabel(item.guidance_stage) }}</span>
            <span>{{ item.resolved ? '已解决' : '进行中' }}</span>
          </button>
          <div class="row-actions conversation-card__actions">
            <button
              class="ghost-button ghost-button--danger"
              :disabled="sending || deletingConversationIds.has(item.id)"
              @click="deleteConversation(item)"
            >
              {{ deletingConversationIds.has(item.id) ? '删除中...' : '删除' }}
            </button>
          </div>
        </article>
      </div>
    </aside>

    <section class="panel chat-panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">Socratic Chat</p>
          <h2>当前阶段：{{ guidanceStageLabel }}</h2>
        </div>
        <button class="ghost-button" :disabled="!currentConversationId" @click="toggleResolved">标记已解决</button>
      </div>

      <div ref="chatStreamRef" class="chat-stream">
        <article v-for="(item, index) in messages" :key="index" :class="['bubble', item.role]">
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
          <div class="message-body" v-html="renderMessageBody(item.content)"></div>
        </article>
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
        <el-select v-model="form.subject" :disabled="sending" placeholder="选择学科" @change="handleSubjectChange">
          <el-option v-for="subject in subjects" :key="subject" :label="subject" :value="subject" />
        </el-select>
        <div class="chat-model-picker" role="radiogroup" aria-label="选择对话模型">
          <button
            v-for="model in chatModels"
            :key="model.key"
            type="button"
            :class="[
              'chat-model-option',
              `chat-model-option--${chatModelStatus(model.key).status}`,
              { 'chat-model-option--active': form.llmModel === model.key },
            ]"
            :disabled="sending || isChatModelUnavailable(model.key)"
            role="radio"
            :aria-checked="form.llmModel === model.key"
            @click="selectChatModel(model.key)"
          >
            <span class="chat-model-option__head">
              <span class="chat-model-option__name">{{ model.name }}</span>
              <span class="chat-model-status">{{ chatModelStatusLabel(model.key) }}</span>
            </span>
            <span class="chat-model-option__description">{{ model.description }}</span>
            <span v-if="chatModelQuotaLabel(model)" class="chat-model-option__quota">
              {{ chatModelQuotaLabel(model) }}
            </span>
          </button>
        </div>
        <el-input
          v-model="form.message"
          :disabled="sending"
          type="textarea"
          :rows="4"
          resize="none"
          placeholder="输入你的问题，系统会先引导你整理思路"
        />
        <div v-if="pendingImagePreviewUrl" class="recommendation-card__images">
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
          <div class="row-actions">
            <button class="ghost-button" :disabled="sending" @click="triggerCameraCapture">重新拍照</button>
            <button class="ghost-button" :disabled="sending" @click="triggerGalleryPicker">从相册替换</button>
            <button class="ghost-button" :disabled="sending" @click="removePendingImage">移除图片</button>
          </div>
        </div>
        <p class="panel-subcopy">
          支持上传 1 张图片，可直接拍照或从相册选择；只有当前新上传图片会进入聊天理解。
        </p>
        <p v-if="sending" class="stream-hint">正在流式生成，可随时停止。</p>
        <div class="chat-actions">
          <button class="ghost-button" :disabled="sending" @click="triggerCameraCapture">拍照</button>
          <button class="ghost-button" :disabled="sending" @click="triggerGalleryPicker">
            {{ pendingImageFile ? '从相册替换' : '从相册选择' }}
          </button>
          <button class="primary-button" :disabled="sending || !canSend" @click="sendMessage">
            {{ sending ? '生成中...' : '发送问题' }}
          </button>
          <button v-if="sending" class="ghost-button" @click="stopStreaming()">停止生成</button>
        </div>
      </div>

      <section class="recommendation-panel">
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
