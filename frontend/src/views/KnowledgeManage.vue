<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import axios from 'axios'
import { ElMessage } from 'element-plus'

import { useAuthorizedAssets } from '../composables/useAuthorizedAssets'
import { api, type KnowledgeAsset } from '../utils/api'
import { collectInlineAssetIds, renderRichText, type InlineRichTextAsset } from '../utils/richText'

interface KnowledgeDoc {
  id: number
  subject: string
  filename: string
  mime_type: string
  size_bytes: number
  resource_type: string
  grade?: number | null
  chapter?: string | null
  section?: string | null
  difficulty?: string | null
  tags: string[]
  status: string
  error_message?: string | null
  created_at: string
}

interface ImportTask {
  id: number
  document_id: number
  celery_task_id?: string | null
  progress: number
  status: string
  error_message?: string | null
  status_message: string
  document_filename?: string | null
  document_subject?: string | null
  created_at: string
  updated_at: string
}

interface KnowledgeChunk {
  id: number
  document_id: number
  chunk_index: number
  content: string
  subject: string
  resource_type: string
  grade?: number | null
  chapter?: string | null
  section?: string | null
  difficulty?: string | null
  tags: string[]
  chunk_kind?: string | null
  question_number?: string | null
  question_text?: string | null
  answer_text?: string | null
  explanation_text?: string | null
  contains_images: boolean
  image_count: number
  assets: KnowledgeAsset[]
}

interface ChunkPreviewSummary {
  totalChunks: number
  questionChunks: number
  answerCount: number
  explanationCount: number
  imageCount: number
  chapterCount: number
  sectionCount: number
  chapters: string[]
  sections: string[]
  splitMode: string
  warnings: string[]
}

const subjectOptions = ['语文', '数学', '英语', '物理', '化学', '生物', '政治', '历史', '地理']
const gradeOptions = [
  { label: '不限年级', value: null },
  { label: '高一', value: 1 },
  { label: '高二', value: 2 },
  { label: '高三', value: 3 },
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
const questionResourceTypes = new Set(['exercise', 'question_set'])
const statusOptions = [
  { value: 'all', label: '全部状态' },
  { value: 'pending', label: '等待中' },
  { value: 'processing', label: '处理中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const uploadForm = reactive({
  subject: '数学',
  resource_type: 'knowledge_note',
  grade: null as number | null,
  chapter: '',
  section: '',
  difficulty: '',
  tags: '',
})

const editDialogVisible = ref(false)
const editSaving = ref(false)
const editingDocumentId = ref<number | null>(null)
const editingOriginalResourceType = ref('knowledge_note')
const editForm = reactive({
  resource_type: 'knowledge_note',
  grade: null as number | null,
  chapter: '',
  section: '',
  difficulty: '',
  tags: '',
})

const batchDialogVisible = ref(false)
const batchSaving = ref(false)
const batchForm = reactive({
  apply_resource_type: false,
  resource_type: 'knowledge_note',
  apply_grade: false,
  grade: null as number | null,
  apply_chapter: false,
  chapter: '',
  apply_section: false,
  section: '',
  apply_difficulty: false,
  difficulty: '',
  apply_tags: false,
  tags: '',
})

const documents = ref<KnowledgeDoc[]>([])
const tasks = ref<ImportTask[]>([])
const latestTask = ref<ImportTask | null>(null)
const previewDialogVisible = ref(false)
const previewLoading = ref(false)
const previewDocumentId = ref<number | null>(null)
const previewChunks = ref<KnowledgeChunk[]>([])
const previewError = ref('')
const selectedDocumentIds = ref<number[]>([])
const taskStatusFilter = ref('all')
const documentSubjectFilter = ref('all')
const documentStatusFilter = ref('all')
const documentResourceTypeFilter = ref('all')
const documentGradeFilter = ref<'all' | number>('all')
const documentDifficultyFilter = ref('all')
const documentKeyword = ref('')
const uploading = ref(false)
const deletingTaskIds = ref<number[]>([])
const deletingDocumentIds = ref<number[]>([])
const chunkCache = reactive<Record<number, KnowledgeChunk[]>>({})
const chunkSummaryCache = reactive<Record<number, ChunkPreviewSummary>>({})
let pollingTimer: number | null = null
const { assetUrl, openAsset, preloadAssets } = useAuthorizedAssets()

const uploadSupportsDifficulty = computed(() => questionResourceTypes.has(uploadForm.resource_type))
const uploadSupportsChapter = computed(() => uploadForm.resource_type !== 'extension')
const editSupportsDifficulty = computed(() => questionResourceTypes.has(editForm.resource_type))
const editSupportsChapter = computed(() => editForm.resource_type !== 'extension')
const batchResourceTypeSupportsDifficulty = computed(() => questionResourceTypes.has(batchForm.resource_type))
const batchResourceTypeSupportsChapter = computed(() => batchForm.resource_type !== 'extension')

const taskSummary = computed(() => ({
  total: tasks.value.length,
  active: tasks.value.filter((item) => isActiveStatus(item.status)).length,
  failed: tasks.value.filter((item) => item.status === 'failed').length,
  completed: tasks.value.filter((item) => item.status === 'completed').length,
  cancelled: tasks.value.filter((item) => item.status === 'cancelled').length,
}))

const documentSummary = computed(() => ({
  total: documents.value.length,
  active: documents.value.filter((item) => documentHasActiveTask(item.id)).length,
  failed: documents.value.filter((item) => item.status === 'failed').length,
  completed: documents.value.filter((item) => item.status === 'completed').length,
  cancelled: documents.value.filter((item) => item.status === 'cancelled').length,
}))

const filteredTasks = computed(() => tasks.value.filter((item) => {
  return taskStatusFilter.value === 'all' || item.status === taskStatusFilter.value
}))

const filteredDocuments = computed(() => {
  const keyword = documentKeyword.value.trim().toLowerCase()
  return documents.value.filter((item) => {
    if (documentSubjectFilter.value !== 'all' && item.subject !== documentSubjectFilter.value) {
      return false
    }
    if (documentStatusFilter.value !== 'all' && item.status !== documentStatusFilter.value) {
      return false
    }
    if (documentResourceTypeFilter.value !== 'all' && item.resource_type !== documentResourceTypeFilter.value) {
      return false
    }
    if (documentGradeFilter.value !== 'all' && item.grade !== documentGradeFilter.value) {
      return false
    }
    if (documentDifficultyFilter.value !== 'all' && item.difficulty !== documentDifficultyFilter.value) {
      return false
    }
    if (keyword) {
      const haystack = [
        item.filename,
        item.chapter || '',
        item.section || '',
        item.error_message || '',
        ...item.tags,
      ]
        .join(' ')
        .toLowerCase()
      if (!haystack.includes(keyword)) {
        return false
      }
    }
    return true
  })
})

const selectedDocuments = computed(() => documents.value.filter((item) => selectedDocumentIds.value.includes(item.id)))
const selectedEditableDocuments = computed(() => selectedDocuments.value.filter((item) => !documentHasActiveTask(item.id)))
const filteredSelectableDocuments = computed(() => filteredDocuments.value.filter((item) => !documentHasActiveTask(item.id)))
const allFilteredSelectableSelected = computed(() => {
  if (!filteredSelectableDocuments.value.length) {
    return false
  }
  return filteredSelectableDocuments.value.every((item) => selectedDocumentIds.value.includes(item.id))
})
const editingDocument = computed(() => documents.value.find((item) => item.id === editingDocumentId.value) || null)
const editNeedsReingest = computed(() => {
  return Boolean(
    editingDocument.value
    && editingDocument.value.status === 'completed'
    && editingOriginalResourceType.value !== editForm.resource_type,
  )
})
const batchNeedsReingest = computed(() => {
  return Boolean(
    batchForm.apply_resource_type
    && selectedEditableDocuments.value.some((item) => item.status === 'completed' && item.resource_type !== batchForm.resource_type),
  )
})
const previewDocument = computed(() => documents.value.find((item) => item.id === previewDocumentId.value) || null)
const previewSummary = computed(() => {
  if (previewDocumentId.value === null) {
    return null
  }
  return chunkSummaryCache[previewDocumentId.value] || null
})
const latestTaskChunkSummary = computed(() => {
  if (!latestTask.value || latestTask.value.status !== 'completed') {
    return null
  }
  return chunkSummaryCache[latestTask.value.document_id] || null
})

function isActiveStatus(status: string) {
  return ['pending', 'processing'].includes(status)
}

function documentHasActiveTask(documentId: number) {
  return tasks.value.some((item) => item.document_id === documentId && isActiveStatus(item.status))
}

function statusLabel(status: string) {
  return statusOptions.find((item) => item.value === status)?.label || status
}

function resourceTypeLabel(value: string) {
  return resourceTypeOptions.find((item) => item.value === value)?.label || value
}

function difficultyLabel(value?: string | null) {
  if (!value) {
    return '-'
  }
  return difficultyOptions.find((item) => item.value === value)?.label || value
}

function formatFileSize(value: number) {
  if (value < 1024) {
    return `${value} B`
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function formatDateTime(value?: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function progressStatus(status: string) {
  if (status === 'failed') {
    return 'exception'
  }
  if (status === 'completed') {
    return 'success'
  }
  return undefined
}

function markBusy(target: typeof deletingTaskIds | typeof deletingDocumentIds, id: number) {
  if (!target.value.includes(id)) {
    target.value = [...target.value, id]
  }
}

function clearBusy(target: typeof deletingTaskIds | typeof deletingDocumentIds, id: number) {
  target.value = target.value.filter((item) => item !== id)
}

function friendlyImportMessage(value?: string | null) {
  const normalized = value?.trim()
  if (!normalized) {
    return '-'
  }
  if (
    normalized.startsWith('任务已创建')
    || normalized.startsWith('开始解析文件')
    || normalized.startsWith('文本提取完成')
    || normalized.startsWith('文本切分完成')
    || normalized.startsWith('已写入数据库')
    || normalized.startsWith('正在写入向量库')
    || normalized.startsWith('向量索引写入完成')
    || normalized.startsWith('导入完成')
    || normalized.startsWith('任务已取消')
    || normalized.startsWith('已请求取消任务')
    || normalized.startsWith('Worker 已开始执行任务')
    || normalized === '任务处理中'
    || normalized === '任务已完成'
    || normalized === '任务失败'
    || normalized === '任务已取消'
  ) {
    return normalized
  }
  if (normalized.includes('文档未提取到可用文本')) {
    return '没有提取到可用文字内容，暂时无法建立索引。'
  }
  if (normalized.includes('扫描版') || normalized.includes('文本层')) {
    return '该 PDF 更像扫描件，当前版本还不能稳定提取文字。请上传可复制文字的 PDF，或转成 Word/TXT 后再上传。'
  }
  if (normalized.includes('DOCX 文件缺少') || normalized.includes('DOCX 文件损坏') || normalized.includes('XML 解析失败')) {
    return 'Word 文件疑似损坏或格式不正确，请重新另存或导出后再上传。'
  }
  if (normalized.includes('暂不支持解析文件类型')) {
    return '当前文件类型暂不支持解析，请上传 PDF、DOCX、TXT、Markdown 或 TeX。'
  }
  if (/加密|encrypted/i.test(normalized)) {
    return '文件可能已加密或受保护，请取消加密后再上传。'
  }
  if (normalized.includes('导入任务或文档不存在')) {
    return '导入任务记录异常，建议删除该记录后重新上传。'
  }
  return '导入失败，请检查文件内容、格式与权限设置后重试。'
}

function hasTechnicalImportDetail(value?: string | null) {
  const normalized = value?.trim()
  return Boolean(normalized && friendlyImportMessage(normalized) !== normalized)
}

function extractApiErrorDetail(error: unknown) {
  if (!axios.isAxiosError(error)) {
    return ''
  }

  const payload = error.response?.data
  if (typeof payload === 'string') {
    return payload.trim()
  }
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail?: unknown }).detail
    if (typeof detail === 'string') {
      return detail.trim()
    }
  }
  return ''
}

function uploadErrorMessage(error: unknown) {
  if (!axios.isAxiosError(error)) {
    return '上传失败'
  }

  const status = error.response?.status
  const detail = extractApiErrorDetail(error)

  if (status === 413 || detail === 'File too large') {
    return '上传失败：文件超过当前上传上限，请控制在 50MB 以内。'
  }
  if (detail === 'Unsupported file type' || detail === 'Unsupported MIME type') {
    return '上传失败：当前仅支持 PDF、DOCX、TXT、Markdown 和 TeX。'
  }
  if (detail) {
    return `上传失败：${detail}`
  }
  if (!error.response) {
    return '上传失败：无法连接服务器'
  }
  return '上传失败'
}

function chunkKindLabel(value?: string | null) {
  if (value === 'question_item') {
    return '题目片段'
  }
  return '通用片段'
}

function uniqueNonEmpty(values: Array<string | null | undefined>) {
  return [...new Set(values.map((item) => (item || '').trim()).filter(Boolean))]
}

function isQuestionChunk(item: KnowledgeChunk) {
  return item.chunk_kind === 'question_item' || Boolean(item.question_number || item.question_text)
}

function buildChunkSummary(document: KnowledgeDoc | null, chunks: KnowledgeChunk[]): ChunkPreviewSummary {
  const questionItems = chunks.filter((item) => isQuestionChunk(item))
  const chapters = uniqueNonEmpty(chunks.map((item) => item.chapter))
  const sections = uniqueNonEmpty(chunks.map((item) => item.section))
  const answerCount = questionItems.filter((item) => item.answer_text?.trim()).length
  const explanationCount = questionItems.filter((item) => item.explanation_text?.trim()).length
  const imageCount = chunks.reduce((total, item) => total + (item.image_count || 0), 0)
  const missingAnswerCount = questionItems.filter((item) => !item.answer_text?.trim()).length
  const missingExplanationCount = questionItems.filter((item) => !item.explanation_text?.trim()).length
  const splitMode = questionItems.length
    ? '按题目拆分'
    : document && questionResourceTypes.has(document.resource_type)
      ? '按段落切分（未识别到稳定题号）'
      : '按段落切分'

  const warnings: string[] = []
  if (document && questionResourceTypes.has(document.resource_type) && !questionItems.length) {
    warnings.push('当前没有识别出按题号拆分结果，这份题库资料实际上按通用段落切分。')
  }
  if (missingAnswerCount) {
    warnings.push(`共 ${missingAnswerCount} 道题暂未识别到答案。`)
  }
  if (missingExplanationCount) {
    warnings.push(`共 ${missingExplanationCount} 道题暂未识别到解析。`)
  }
  if (!chapters.length && !sections.length && document?.resource_type !== 'extension') {
    warnings.push('暂未识别出明确章节结构，建议抽查片段确认内容组织是否符合预期。')
  }

  return {
    totalChunks: chunks.length,
    questionChunks: questionItems.length,
    answerCount,
    explanationCount,
    imageCount,
    chapterCount: chapters.length,
    sectionCount: sections.length,
    chapters,
    sections,
    splitMode,
    warnings,
  }
}

function cacheChunkPreview(documentId: number, chunks: KnowledgeChunk[]) {
  chunkCache[documentId] = chunks
  const document = documents.value.find((item) => item.id === documentId) || null
  chunkSummaryCache[documentId] = buildChunkSummary(document, chunks)
}

function refreshCachedChunkSummaries() {
  Object.entries(chunkCache).forEach(([documentId, chunks]) => {
    const document = documents.value.find((item) => item.id === Number(documentId)) || null
    chunkSummaryCache[Number(documentId)] = buildChunkSummary(document, chunks)
  })
}

async function fetchDocumentChunks(documentId: number, options: { force?: boolean } = {}) {
  if (!options.force && documentId in chunkCache) {
    return chunkCache[documentId]
  }
  const { data } = await api.get<KnowledgeChunk[]>(`/knowledge/documents/${documentId}/chunks`)
  cacheChunkPreview(documentId, data)
  return data
}

async function ensureLatestTaskSummary(task: ImportTask | null) {
  if (!task || task.status !== 'completed') {
    return
  }
  if (task.document_id in chunkSummaryCache) {
    return
  }
  try {
    await fetchDocumentChunks(task.document_id)
  } catch (error) {
    console.error(error)
  }
}

async function openChunkPreview(document: KnowledgeDoc) {
  if (document.status !== 'completed') {
    ElMessage.info('资料尚未完成导入，当前没有可预览的切分结果')
    return
  }
  previewDialogVisible.value = true
  previewLoading.value = true
  previewDocumentId.value = document.id
  previewError.value = ''
  try {
    previewChunks.value = await fetchDocumentChunks(document.id)
    await preloadAssets(previewChunks.value.flatMap((item) => item.assets))
  } catch (error) {
    console.error(error)
    previewChunks.value = []
    previewError.value = '切分结果加载失败，请稍后重试'
  } finally {
    previewLoading.value = false
  }
}

async function openTaskChunkPreview(task: ImportTask) {
  const document = documents.value.find((item) => item.id === task.document_id)
  if (!document) {
    ElMessage.error('未找到对应资料记录')
    return
  }
  await openChunkPreview(document)
}

function imageAssets(chunk: KnowledgeChunk) {
  const inlineAssetIds = collectInlineAssetIds(
    [chunk.question_text, chunk.answer_text, chunk.explanation_text, chunk.content].filter(Boolean).join('\n'),
    chunk.assets,
  )
  return chunk.assets.filter((asset) => asset.content_type.startsWith('image/') && !inlineAssetIds.has(asset.asset_id))
}

function otherAssets(chunk: KnowledgeChunk) {
  return chunk.assets.filter((asset) => !asset.content_type.startsWith('image/'))
}

function buildInlineAssets(assets: KnowledgeAsset[]): InlineRichTextAsset[] {
  return assets.map((asset) => ({
    asset,
    src: asset.content_type.startsWith('image/') ? assetUrl(asset) : '',
  }))
}

function renderChunkBody(content: string, assets: KnowledgeAsset[] = []): string {
  return renderRichText(content, { inlineAssets: buildInlineAssets(assets) })
}

function normalizeTagsInput(value: string) {
  return value
    .split(/[,\n，]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function sanitizeUploadMetadata() {
  if (!uploadSupportsDifficulty.value) {
    uploadForm.difficulty = ''
  }
  if (!uploadSupportsChapter.value) {
    uploadForm.chapter = ''
    uploadForm.section = ''
  }
}

function sanitizeEditMetadata() {
  if (!editSupportsDifficulty.value) {
    editForm.difficulty = ''
  }
  if (!editSupportsChapter.value) {
    editForm.chapter = ''
    editForm.section = ''
  }
}

function sanitizeBatchMetadata() {
  if (!batchResourceTypeSupportsDifficulty.value) {
    batchForm.difficulty = ''
  }
  if (!batchResourceTypeSupportsChapter.value) {
    batchForm.chapter = ''
    batchForm.section = ''
  }
}

function resetBatchForm() {
  Object.assign(batchForm, {
    apply_resource_type: false,
    resource_type: 'knowledge_note',
    apply_grade: false,
    grade: null,
    apply_chapter: false,
    chapter: '',
    apply_section: false,
    section: '',
    apply_difficulty: false,
    difficulty: '',
    apply_tags: false,
    tags: '',
  })
}

function syncSelectedDocuments() {
  const selectableIds = new Set(
    documents.value.filter((item) => !documentHasActiveTask(item.id)).map((item) => item.id),
  )
  selectedDocumentIds.value = selectedDocumentIds.value.filter((id) => selectableIds.has(id))
}

function selectFilteredDocuments() {
  const next = new Set(selectedDocumentIds.value)
  filteredSelectableDocuments.value.forEach((item) => next.add(item.id))
  selectedDocumentIds.value = [...next]
}

function clearSelectedDocuments() {
  selectedDocumentIds.value = []
}

async function loadDocuments() {
  const { data } = await api.get<KnowledgeDoc[]>('/knowledge/documents')
  documents.value = data
}

async function loadTasks() {
  const { data } = await api.get<ImportTask[]>('/knowledge/tasks', { params: { limit: 100 } })
  tasks.value = data
  latestTask.value = data[0] || null
}

async function refreshData(silent = false) {
  try {
    await Promise.all([loadTasks(), loadDocuments()])
    syncSelectedDocuments()
    refreshCachedChunkSummaries()
  } catch (error) {
    console.error(error)
    if (!silent) {
      ElMessage.error('知识库数据加载失败')
    }
  }
}

function startPolling() {
  stopPolling()
  pollingTimer = window.setInterval(async () => {
    await refreshData(true)
    const active = tasks.value.some((item) => isActiveStatus(item.status))
    if (!active) {
      stopPolling()
    }
  }, 2000)
}

function stopPolling() {
  if (pollingTimer) {
    window.clearInterval(pollingTimer)
    pollingTimer = null
  }
}

async function handleUpload(uploadFile: File) {
  uploading.value = true
  sanitizeUploadMetadata()
  try {
    const formData = new FormData()
    formData.append('file', uploadFile)
    formData.append('resource_type', uploadForm.resource_type)
    if (uploadForm.grade !== null) {
      formData.append('grade', String(uploadForm.grade))
    }
    if (uploadForm.chapter.trim()) {
      formData.append('chapter', uploadForm.chapter.trim())
    }
    if (uploadForm.section.trim()) {
      formData.append('section', uploadForm.section.trim())
    }
    if (uploadForm.difficulty) {
      formData.append('difficulty', uploadForm.difficulty)
    }
    if (uploadForm.tags.trim()) {
      formData.append('tags', uploadForm.tags.trim())
    }
    const { data } = await api.post<ImportTask>(`/knowledge/upload?subject=${encodeURIComponent(uploadForm.subject)}`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    latestTask.value = data
    await refreshData(true)
    startPolling()
    ElMessage.success('上传成功，已进入导入队列')
  } catch (error) {
    console.error(error)
    ElMessage.error(uploadErrorMessage(error))
  } finally {
    uploading.value = false
  }
}

async function cancelTask(taskId: number) {
  try {
    await api.post(`/knowledge/tasks/${taskId}/cancel`)
    await refreshData(true)
    ElMessage.success('已请求取消任务')
  } catch (error) {
    console.error(error)
    ElMessage.error('取消失败')
  }
}

async function deleteTask(task: ImportTask) {
  if (isActiveStatus(task.status)) {
    ElMessage.error('进行中的任务不能直接删除，请先取消')
    return
  }
  const taskLabel = task.document_filename || `文档 #${task.document_id}`
  if (!window.confirm(`确认删除任务记录“${taskLabel}”？此操作不会删除已上传资料。`)) {
    return
  }

  markBusy(deletingTaskIds, task.id)
  try {
    await api.delete(`/knowledge/tasks/${task.id}`)
    await refreshData(true)
    ElMessage.success('任务记录已删除')
  } catch (error) {
    console.error(error)
    ElMessage.error('删除任务失败')
  } finally {
    clearBusy(deletingTaskIds, task.id)
  }
}

function openEditDialog(document: KnowledgeDoc) {
  editingDocumentId.value = document.id
  editingOriginalResourceType.value = document.resource_type || 'knowledge_note'
  Object.assign(editForm, {
    resource_type: document.resource_type || 'knowledge_note',
    grade: document.grade ?? null,
    chapter: document.chapter || '',
    section: document.section || '',
    difficulty: document.difficulty || '',
    tags: document.tags.join(', '),
  })
  sanitizeEditMetadata()
  editDialogVisible.value = true
}

async function saveDocumentMetadata() {
  if (editingDocumentId.value === null) {
    return
  }
  editSaving.value = true
  sanitizeEditMetadata()
  const needsReingest = editNeedsReingest.value
  try {
    await api.put(`/knowledge/documents/${editingDocumentId.value}`, {
      resource_type: editForm.resource_type,
      grade: editForm.grade,
      chapter: editForm.chapter || null,
      section: editForm.section || null,
      difficulty: editForm.difficulty || null,
      tags: normalizeTagsInput(editForm.tags),
    })
    editDialogVisible.value = false
    await refreshData(true)
    ElMessage.success(
      needsReingest
        ? '资料 metadata 已更新；资料类型变化不会自动重新切分，如需按新类型处理请删除后重新上传'
        : '资料 metadata 已更新',
    )
  } catch (error) {
    console.error(error)
    ElMessage.error('保存 metadata 失败')
  } finally {
    editSaving.value = false
  }
}

function openBatchDialog() {
  if (!selectedEditableDocuments.value.length) {
    ElMessage.error('请先选择至少一份可编辑资料')
    return
  }
  resetBatchForm()
  batchDialogVisible.value = true
}

async function saveBatchMetadata() {
  if (!selectedEditableDocuments.value.length) {
    ElMessage.error('没有可批量编辑的资料')
    return
  }

  sanitizeBatchMetadata()
  const needsReingest = batchNeedsReingest.value
  const payload: Record<string, unknown> = {
    document_ids: selectedEditableDocuments.value.map((item) => item.id),
  }
  let fieldCount = 0

  if (batchForm.apply_resource_type) {
    payload.resource_type = batchForm.resource_type
    fieldCount += 1
  }
  if (batchForm.apply_grade) {
    payload.grade = batchForm.grade
    fieldCount += 1
  }
  if (batchForm.apply_chapter) {
    payload.chapter = batchForm.chapter.trim() || null
    fieldCount += 1
  }
  if (batchForm.apply_section) {
    payload.section = batchForm.section.trim() || null
    fieldCount += 1
  }
  if (batchForm.apply_difficulty) {
    payload.difficulty = batchForm.difficulty || null
    fieldCount += 1
  }
  if (batchForm.apply_tags) {
    payload.tags = normalizeTagsInput(batchForm.tags)
    fieldCount += 1
  }

  if (!fieldCount) {
    ElMessage.error('请至少勾选一个要批量更新的字段')
    return
  }

  batchSaving.value = true
  try {
    await api.put('/knowledge/documents/bulk', payload)
    batchDialogVisible.value = false
    await refreshData(true)
    ElMessage.success(
      needsReingest
        ? `已批量更新 ${selectedEditableDocuments.value.length} 份资料；资料类型变化不会自动重新切分，如需按新类型处理请删除后重新上传`
        : `已批量更新 ${selectedEditableDocuments.value.length} 份资料`,
    )
  } catch (error) {
    console.error(error)
    ElMessage.error('批量更新失败')
  } finally {
    batchSaving.value = false
  }
}

async function deleteDocument(document: KnowledgeDoc) {
  if (documentHasActiveTask(document.id)) {
    ElMessage.error('资料仍在导入中，请先取消任务')
    return
  }
  if (!window.confirm(`确认删除资料“${document.filename}”？这会同时删除原文件、索引片段和关联任务记录。`)) {
    return
  }

  markBusy(deletingDocumentIds, document.id)
  try {
    await api.delete(`/knowledge/documents/${document.id}`)
    delete chunkCache[document.id]
    delete chunkSummaryCache[document.id]
    await refreshData(true)
    ElMessage.success('资料已删除')
  } catch (error) {
    console.error(error)
    ElMessage.error('删除资料失败')
  } finally {
    clearBusy(deletingDocumentIds, document.id)
  }
}

watch(
  () => latestTask.value ? `${latestTask.value.document_id}-${latestTask.value.status}` : '',
  () => {
    void ensureLatestTaskSummary(latestTask.value)
  },
  { immediate: true },
)

onMounted(async () => {
  await refreshData()
  await ensureLatestTaskSummary(latestTask.value)
  if (tasks.value.some((item) => isActiveStatus(item.status))) {
    startPolling()
  }
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<template>
  <section class="dashboard-stack">
    <section class="panel">
      <div class="panel-header panel-header--stack">
        <div>
          <p class="eyebrow">Knowledge Base</p>
          <h2>资料上传与任务跟踪</h2>
        </div>
        <button class="ghost-button" @click="refreshData()">刷新</button>
      </div>
      <div class="knowledge-meta-grid">
        <el-select v-model="uploadForm.subject" placeholder="选择学科">
          <el-option v-for="item in subjectOptions" :key="item" :label="item" :value="item" />
        </el-select>
        <el-select v-model="uploadForm.resource_type" placeholder="资料类型" @change="sanitizeUploadMetadata">
          <el-option v-for="item in resourceTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-select v-model="uploadForm.grade" placeholder="适用年级">
          <el-option v-for="item in gradeOptions" :key="item.label" :label="item.label" :value="item.value" />
        </el-select>
        <el-select
          v-if="uploadSupportsDifficulty"
          v-model="uploadForm.difficulty"
          clearable
          placeholder="题目难度"
        >
          <el-option v-for="item in difficultyOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-input
          v-if="uploadSupportsChapter"
          v-model="uploadForm.chapter"
          placeholder="章节，例如：第二章 机械运动"
        />
        <el-input
          v-if="uploadSupportsChapter"
          v-model="uploadForm.section"
          placeholder="小节，例如：2.1 匀变速直线运动"
        />
        <el-input
          v-model="uploadForm.tags"
          class="knowledge-meta-grid__wide"
          placeholder="标签，多个标签用逗号分隔"
        />
      </div>
      <div class="toolbar toolbar-wrap toolbar-end">
        <el-upload
          :show-file-list="false"
          :auto-upload="false"
          accept=".pdf,.docx,.txt,.md,.tex"
          :on-change="(file) => handleUpload(file.raw!)"
        >
          <button class="primary-button" :disabled="uploading">{{ uploading ? '上传中...' : '选择资料并上传' }}</button>
        </el-upload>
      </div>
      <div class="detail-chip-group">
        <span class="detail-chip">任务总数 {{ taskSummary.total }}</span>
        <span class="detail-chip">进行中 {{ taskSummary.active }}</span>
        <span class="detail-chip">已完成 {{ taskSummary.completed }}</span>
        <span class="detail-chip">失败 {{ taskSummary.failed }}</span>
        <span class="detail-chip">已取消 {{ taskSummary.cancelled }}</span>
      </div>
      <div v-if="latestTask" class="task-card">
        <div class="task-card-head">
          <strong>最新任务</strong>
          <span>{{ latestTask.document_filename || `文档 #${latestTask.document_id}` }}</span>
        </div>
        <el-progress :percentage="latestTask.progress" :status="progressStatus(latestTask.status)" />
        <p>{{ friendlyImportMessage(latestTask.status_message) }}</p>
        <div v-if="latestTaskChunkSummary" class="detail-chip-group">
          <span class="detail-chip">{{ latestTaskChunkSummary.splitMode }}</span>
          <span class="detail-chip">片段 {{ latestTaskChunkSummary.totalChunks }}</span>
          <span v-if="latestTaskChunkSummary.questionChunks" class="detail-chip">题目 {{ latestTaskChunkSummary.questionChunks }}</span>
          <span v-if="latestTaskChunkSummary.answerCount" class="detail-chip">答案 {{ latestTaskChunkSummary.answerCount }}</span>
          <span v-if="latestTaskChunkSummary.explanationCount" class="detail-chip">解析 {{ latestTaskChunkSummary.explanationCount }}</span>
          <span v-if="latestTaskChunkSummary.chapterCount" class="detail-chip">章节 {{ latestTaskChunkSummary.chapterCount }}</span>
          <span v-if="latestTaskChunkSummary.sectionCount" class="detail-chip">小节 {{ latestTaskChunkSummary.sectionCount }}</span>
          <span v-if="latestTaskChunkSummary.imageCount" class="detail-chip">图片 {{ latestTaskChunkSummary.imageCount }} 张</span>
        </div>
        <div class="toolbar toolbar-wrap">
          <div class="detail-chip-group">
            <span class="detail-chip">{{ latestTask.document_subject || '-' }}</span>
            <span class="detail-chip">{{ statusLabel(latestTask.status) }}</span>
            <span class="detail-chip">{{ formatDateTime(latestTask.updated_at) }}</span>
          </div>
          <div class="row-actions">
            <button
              v-if="isActiveStatus(latestTask.status)"
              class="ghost-button"
              @click="cancelTask(latestTask.id)"
            >
              取消任务
            </button>
            <button
              v-else
              class="ghost-button"
              :disabled="deletingTaskIds.includes(latestTask.id)"
              @click="deleteTask(latestTask)"
            >
              {{ deletingTaskIds.includes(latestTask.id) ? '删除中...' : '删除记录' }}
            </button>
            <button
              class="ghost-button"
              :disabled="latestTask.status !== 'completed'"
              @click="openTaskChunkPreview(latestTask)"
            >
              查看切分
            </button>
          </div>
        </div>
        <details v-if="hasTechnicalImportDetail(latestTask.error_message)" class="detail-disclosure">
          <summary>技术详情</summary>
          <pre class="mono-block">{{ latestTask.error_message }}</pre>
        </details>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header panel-header--stack">
        <div>
          <p class="eyebrow">Task Queue</p>
          <h2>导入任务列表</h2>
        </div>
        <div class="toolbar toolbar-wrap toolbar-end">
          <el-select v-model="taskStatusFilter" class="toolbar-field" placeholder="筛选任务状态">
            <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </div>
      </div>
      <div class="table-like">
        <article v-for="item in filteredTasks" :key="item.id" class="task-row">
          <div class="task-main">
            <strong>{{ item.document_filename || `文档 #${item.document_id}` }}</strong>
            <span>{{ item.document_subject || '-' }} · {{ statusLabel(item.status) }}</span>
            <span>{{ friendlyImportMessage(item.status_message) }}</span>
            <span>更新时间 {{ formatDateTime(item.updated_at) }}</span>
          </div>
          <div class="task-side task-side--stack">
            <el-progress :percentage="item.progress" :stroke-width="10" :status="progressStatus(item.status)" />
            <div class="row-actions">
              <button
                v-if="isActiveStatus(item.status)"
                class="ghost-button"
                @click="cancelTask(item.id)"
              >
                取消
              </button>
              <button
                v-else
                class="ghost-button"
                :disabled="deletingTaskIds.includes(item.id)"
                @click="deleteTask(item)"
              >
                {{ deletingTaskIds.includes(item.id) ? '删除中...' : '删除记录' }}
              </button>
              <button
                class="ghost-button"
                :disabled="item.status !== 'completed'"
                @click="openTaskChunkPreview(item)"
              >
                查看切分
              </button>
            </div>
            <details v-if="hasTechnicalImportDetail(item.error_message)" class="detail-disclosure">
              <summary>技术详情</summary>
              <pre class="mono-block">{{ item.error_message }}</pre>
            </details>
          </div>
        </article>
        <p v-if="!filteredTasks.length" class="panel-subcopy">暂无匹配的任务记录。</p>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header panel-header--stack">
        <div>
          <p class="eyebrow">Document List</p>
          <h2>已上传资料管理</h2>
        </div>
        <div class="toolbar toolbar-wrap toolbar-end">
          <el-select v-model="documentSubjectFilter" class="toolbar-field" placeholder="筛选学科">
            <el-option label="全部学科" value="all" />
            <el-option v-for="item in subjectOptions" :key="item" :label="item" :value="item" />
          </el-select>
          <el-select v-model="documentStatusFilter" class="toolbar-field" placeholder="筛选状态">
            <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-select v-model="documentResourceTypeFilter" class="toolbar-field" placeholder="筛选资料类型">
            <el-option label="全部类型" value="all" />
            <el-option v-for="item in resourceTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-select v-model="documentGradeFilter" class="toolbar-field" placeholder="筛选年级">
            <el-option label="全部年级" value="all" />
            <el-option v-for="item in gradeOptions.slice(1)" :key="item.label" :label="item.label" :value="item.value" />
          </el-select>
          <el-select v-model="documentDifficultyFilter" class="toolbar-field" placeholder="筛选难度">
            <el-option label="全部难度" value="all" />
            <el-option v-for="item in difficultyOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-input
            v-model="documentKeyword"
            clearable
            class="toolbar-field toolbar-field--wide"
            placeholder="按文件名、章节、小节、标签搜索"
          />
        </div>
      </div>
      <div class="detail-chip-group">
        <span class="detail-chip">资料总数 {{ documentSummary.total }}</span>
        <span class="detail-chip">进行中 {{ documentSummary.active }}</span>
        <span class="detail-chip">已完成 {{ documentSummary.completed }}</span>
        <span class="detail-chip">失败 {{ documentSummary.failed }}</span>
        <span class="detail-chip">已取消 {{ documentSummary.cancelled }}</span>
      </div>
      <div class="selection-toolbar">
        <div class="detail-chip-group">
          <span class="detail-chip">已选 {{ selectedEditableDocuments.length }} 份可编辑资料</span>
          <span class="detail-chip">当前筛选结果可选 {{ filteredSelectableDocuments.length }} 份</span>
          <span class="detail-chip">导入中的资料需先取消任务后再编辑</span>
        </div>
        <div class="row-actions">
          <button
            class="ghost-button"
            :disabled="!filteredSelectableDocuments.length || allFilteredSelectableSelected"
            @click="selectFilteredDocuments"
          >
            {{ allFilteredSelectableSelected ? '当前筛选结果已全选' : '选中当前筛选结果' }}
          </button>
          <button class="ghost-button" :disabled="!selectedDocumentIds.length" @click="clearSelectedDocuments">
            清空选择
          </button>
          <button class="primary-button" :disabled="!selectedEditableDocuments.length" @click="openBatchDialog">
            批量编辑 metadata
          </button>
        </div>
      </div>
      <div class="table-like">
        <article v-for="item in filteredDocuments" :key="item.id" class="table-row table-row-wrap table-row-selectable">
          <label class="selection-toggle">
            <input
              v-model="selectedDocumentIds"
              type="checkbox"
              :value="item.id"
              :disabled="documentHasActiveTask(item.id)"
            >
            <span>{{ documentHasActiveTask(item.id) ? '导入中' : '选择' }}</span>
          </label>
          <div class="table-main table-main--grow">
            <strong>{{ item.filename }}</strong>
            <span>{{ item.subject }} · {{ resourceTypeLabel(item.resource_type) }} · {{ item.mime_type }}</span>
            <span>上传时间 {{ formatDateTime(item.created_at) }}</span>
            <span v-if="item.error_message">{{ friendlyImportMessage(item.error_message) }}</span>
          </div>
          <div class="row-actions row-actions--wide row-actions--grow">
            <div class="detail-chip-group">
              <span class="detail-chip">{{ formatFileSize(item.size_bytes) }}</span>
              <span class="detail-chip">{{ statusLabel(item.status) }}</span>
              <span v-if="item.grade" class="detail-chip">{{ item.grade }}年级</span>
              <span v-if="item.chapter" class="detail-chip">{{ item.chapter }}</span>
              <span v-if="item.section" class="detail-chip">{{ item.section }}</span>
              <span v-if="item.difficulty" class="detail-chip">难度 {{ difficultyLabel(item.difficulty) }}</span>
              <span v-for="tag in item.tags.slice(0, 4)" :key="`${item.id}-${tag}`" class="detail-chip">#{{ tag }}</span>
            </div>
            <div class="row-actions">
              <button
                class="ghost-button"
                :disabled="documentHasActiveTask(item.id)"
                @click="openEditDialog(item)"
              >
                编辑 metadata
              </button>
              <button
                class="ghost-button"
                :disabled="item.status !== 'completed'"
                @click="openChunkPreview(item)"
              >
                查看切分
              </button>
              <button
                class="ghost-button"
                :disabled="documentHasActiveTask(item.id) || deletingDocumentIds.includes(item.id)"
                @click="deleteDocument(item)"
              >
                {{
                  deletingDocumentIds.includes(item.id)
                    ? '删除中...'
                    : documentHasActiveTask(item.id)
                      ? '导入中'
                      : '删除资料'
                }}
              </button>
            </div>
            <details v-if="hasTechnicalImportDetail(item.error_message)" class="detail-disclosure">
              <summary>技术详情</summary>
              <pre class="mono-block">{{ item.error_message }}</pre>
            </details>
          </div>
        </article>
        <p v-if="!filteredDocuments.length" class="panel-subcopy">暂无匹配的资料记录。</p>
      </div>
    </section>

    <el-dialog v-model="batchDialogVisible" title="批量编辑资料 metadata" width="760px">
      <p class="panel-subcopy">
        将对已选中的 {{ selectedEditableDocuments.length }} 份资料生效。未勾选的字段保持不变；勾选后留空表示清空。
      </p>
      <p class="panel-subcopy">
        章节/小节对拓展资料会自动忽略，难度仅对习题例题和题库试卷生效。
      </p>
      <div v-if="batchNeedsReingest" class="warning-banner">
        你正在批量修改资料类型。注意：metadata 更新不会重新切分历史内容，如需按新类型重建切分结果，请删除后重新上传。
      </div>
      <div class="batch-edit-grid">
        <label class="batch-edit-field">
          <span class="batch-edit-toggle"><input v-model="batchForm.apply_resource_type" type="checkbox"> 更新资料类型</span>
          <el-select
            v-model="batchForm.resource_type"
            placeholder="资料类型"
            :disabled="!batchForm.apply_resource_type"
            @change="sanitizeBatchMetadata"
          >
            <el-option v-for="item in resourceTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </label>
        <label class="batch-edit-field">
          <span class="batch-edit-toggle"><input v-model="batchForm.apply_grade" type="checkbox"> 更新年级</span>
          <el-select v-model="batchForm.grade" placeholder="适用年级" :disabled="!batchForm.apply_grade">
            <el-option v-for="item in gradeOptions" :key="item.label" :label="item.label" :value="item.value" />
          </el-select>
        </label>
        <label class="batch-edit-field">
          <span class="batch-edit-toggle"><input v-model="batchForm.apply_chapter" type="checkbox"> 更新章节</span>
          <el-input
            v-model="batchForm.chapter"
            placeholder="留空可清空章节"
            :disabled="!batchForm.apply_chapter || (batchForm.apply_resource_type && !batchResourceTypeSupportsChapter)"
          />
        </label>
        <label class="batch-edit-field">
          <span class="batch-edit-toggle"><input v-model="batchForm.apply_section" type="checkbox"> 更新小节</span>
          <el-input
            v-model="batchForm.section"
            placeholder="留空可清空小节"
            :disabled="!batchForm.apply_section || (batchForm.apply_resource_type && !batchResourceTypeSupportsChapter)"
          />
        </label>
        <label class="batch-edit-field">
          <span class="batch-edit-toggle"><input v-model="batchForm.apply_difficulty" type="checkbox"> 更新难度</span>
          <el-select
            v-model="batchForm.difficulty"
            clearable
            placeholder="题目难度"
            :disabled="!batchForm.apply_difficulty || (batchForm.apply_resource_type && !batchResourceTypeSupportsDifficulty)"
          >
            <el-option v-for="item in difficultyOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </label>
        <label class="batch-edit-field batch-edit-field--wide">
          <span class="batch-edit-toggle"><input v-model="batchForm.apply_tags" type="checkbox"> 更新标签</span>
          <el-input
            v-model="batchForm.tags"
            placeholder="多个标签用逗号分隔；留空可清空全部标签"
            :disabled="!batchForm.apply_tags"
          />
        </label>
      </div>
      <template #footer>
        <div class="row-actions">
          <button class="ghost-button" @click="batchDialogVisible = false">取消</button>
          <button class="primary-button" :disabled="batchSaving" @click="saveBatchMetadata">
            {{ batchSaving ? '保存中...' : '批量保存 metadata' }}
          </button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="editDialogVisible" title="编辑资料 metadata" width="720px">
      <div v-if="editNeedsReingest" class="warning-banner">
        你正在修改资料类型。注意：保存后只会更新 metadata，不会自动重新切分内容；如需按新类型处理，请删除后重新上传。
      </div>
      <div class="knowledge-meta-grid">
        <el-select v-model="editForm.resource_type" placeholder="资料类型" @change="sanitizeEditMetadata">
          <el-option v-for="item in resourceTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-select v-model="editForm.grade" placeholder="适用年级">
          <el-option v-for="item in gradeOptions" :key="item.label" :label="item.label" :value="item.value" />
        </el-select>
        <el-select
          v-if="editSupportsDifficulty"
          v-model="editForm.difficulty"
          clearable
          placeholder="题目难度"
        >
          <el-option v-for="item in difficultyOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-input
          v-if="editSupportsChapter"
          v-model="editForm.chapter"
          placeholder="章节，例如：第二章 机械运动"
        />
        <el-input
          v-if="editSupportsChapter"
          v-model="editForm.section"
          placeholder="小节，例如：2.1 匀变速直线运动"
        />
        <el-input
          v-model="editForm.tags"
          class="knowledge-meta-grid__wide"
          placeholder="标签，多个标签用逗号分隔"
        />
      </div>
      <template #footer>
        <div class="row-actions">
          <button class="ghost-button" @click="editDialogVisible = false">取消</button>
          <button class="primary-button" :disabled="editSaving" @click="saveDocumentMetadata">
            {{ editSaving ? '保存中...' : '保存 metadata' }}
          </button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="previewDialogVisible" title="切分结果预览" width="1120px">
      <div v-if="previewDocument" class="preview-dialog">
        <div class="preview-summary">
          <div class="task-card-head">
            <strong>{{ previewDocument.filename }}</strong>
            <span>{{ previewDocument.subject }} · {{ resourceTypeLabel(previewDocument.resource_type) }}</span>
          </div>
          <div class="detail-chip-group">
            <span v-if="previewSummary" class="detail-chip">{{ previewSummary.splitMode }}</span>
            <span v-if="previewSummary" class="detail-chip">片段 {{ previewSummary.totalChunks }}</span>
            <span v-if="previewSummary?.questionChunks" class="detail-chip">题目 {{ previewSummary.questionChunks }}</span>
            <span v-if="previewSummary?.answerCount" class="detail-chip">答案 {{ previewSummary.answerCount }}</span>
            <span v-if="previewSummary?.explanationCount" class="detail-chip">解析 {{ previewSummary.explanationCount }}</span>
            <span v-if="previewSummary?.chapterCount" class="detail-chip">章节 {{ previewSummary.chapterCount }}</span>
            <span v-if="previewSummary?.sectionCount" class="detail-chip">小节 {{ previewSummary.sectionCount }}</span>
            <span v-if="previewSummary?.imageCount" class="detail-chip">图片 {{ previewSummary.imageCount }} 张</span>
          </div>
          <p class="panel-subcopy">
            这里展示的是系统实际写入索引前的片段内容，可用于检查题干、答案、解析、图片和章节识别是否正确。
          </p>
          <div v-if="previewSummary?.warnings.length" class="warning-banner">
            <p v-for="warning in previewSummary.warnings" :key="warning">{{ warning }}</p>
          </div>
          <div v-if="previewSummary?.chapters.length" class="detail-chip-group">
            <span class="detail-chip">识别章节</span>
            <span
              v-for="chapter in previewSummary.chapters.slice(0, 6)"
              :key="chapter"
              class="detail-chip"
            >
              {{ chapter }}
            </span>
            <span v-if="previewSummary.chapters.length > 6" class="detail-chip">
              +{{ previewSummary.chapters.length - 6 }}
            </span>
          </div>
          <div v-if="previewSummary?.sections.length" class="detail-chip-group">
            <span class="detail-chip">识别小节</span>
            <span
              v-for="section in previewSummary.sections.slice(0, 6)"
              :key="section"
              class="detail-chip"
            >
              {{ section }}
            </span>
            <span v-if="previewSummary.sections.length > 6" class="detail-chip">
              +{{ previewSummary.sections.length - 6 }}
            </span>
          </div>
        </div>

        <p v-if="previewLoading" class="panel-subcopy">正在加载切分结果...</p>
        <p v-else-if="previewError" class="panel-subcopy">{{ previewError }}</p>
        <p v-else-if="!previewChunks.length" class="panel-subcopy">当前资料没有可展示的切分片段。</p>

        <div v-else class="preview-chunk-list">
          <article v-for="item in previewChunks" :key="item.id" class="preview-chunk-card">
            <div class="task-card-head">
              <strong>片段 {{ item.chunk_index + 1 }}</strong>
              <span>{{ chunkKindLabel(item.chunk_kind) }}</span>
            </div>
            <div class="detail-chip-group">
              <span v-if="item.question_number" class="detail-chip">第 {{ item.question_number }} 题</span>
              <span v-if="item.chapter" class="detail-chip">{{ item.chapter }}</span>
              <span v-if="item.section" class="detail-chip">{{ item.section }}</span>
              <span v-if="item.difficulty" class="detail-chip">难度 {{ difficultyLabel(item.difficulty) }}</span>
              <span v-if="item.contains_images" class="detail-chip">图片 {{ item.image_count }} 张</span>
              <span v-for="tag in item.tags.slice(0, 4)" :key="`${item.id}-${tag}`" class="detail-chip">#{{ tag }}</span>
            </div>

            <div v-if="item.question_text" class="preview-field">
              <strong>题干</strong>
              <div class="message-body" v-html="renderChunkBody(item.question_text, item.assets)"></div>
            </div>
            <div v-if="item.answer_text" class="preview-field">
              <strong>答案</strong>
              <div class="message-body" v-html="renderChunkBody(item.answer_text, item.assets)"></div>
            </div>
            <div v-if="item.explanation_text" class="preview-field">
              <strong>解析</strong>
              <div class="message-body" v-html="renderChunkBody(item.explanation_text, item.assets)"></div>
            </div>
            <div class="preview-field">
              <strong>索引内容</strong>
              <div class="message-body" v-html="renderChunkBody(item.content, item.assets)"></div>
            </div>

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
      </div>
    </el-dialog>
  </section>
</template>
