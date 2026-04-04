<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { useAuthorizedAssets } from '../composables/useAuthorizedAssets'
import {
  api,
  fetchQuestionRecommendations,
  streamChat,
  type KnowledgeAsset,
  type QuestionRecommendation,
} from '../utils/api'
import { collectInlineAssetIds, renderRichText, type InlineRichTextAsset } from '../utils/richText'

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

interface ConversationSummary {
  id: number
  subject: string
  topic: string
  guidance_stage: string
  resolved: boolean
}

const RECOMMENDATION_FETCH_LIMIT = 6
const RECOMMENDATION_PAGE_SIZE = 3
const GUIDANCE_STAGE_LABELS: Record<string, string> = {
  initial_guidance: '初始引导',
  scaffold_hint: '逐步提示',
  fallback_walkthrough: '兜底讲解',
}
const subjects = ['语文', '数学', '英语', '物理', '化学', '生物', '政治', '历史', '地理']
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
const form = reactive({
  subject: '数学',
  message: '',
})
const conversations = ref<ConversationSummary[]>([])
const messages = ref<ChatMessage[]>([])
const currentConversationId = ref<number | null>(null)
const sending = ref(false)
const guidanceStage = ref('initial_guidance')
const recommendationPool = ref<QuestionRecommendation[]>([])
const recommendationOffset = ref(0)
const recommendationSeed = ref('')
const recommendationLoading = ref(false)
const recommendationError = ref('')
const chatStreamRef = ref<HTMLElement | null>(null)
let streamAbortController: AbortController | null = null
let stopRequested = false
const { assetUrl, openAsset, preloadAssets } = useAuthorizedAssets()

const visibleRecommendations = computed(() => (
  recommendationPool.value.slice(recommendationOffset.value, recommendationOffset.value + RECOMMENDATION_PAGE_SIZE)
))

const hasRecommendations = computed(() => visibleRecommendations.value.length > 0)
const guidanceStageLabel = computed(() => stageLabel(guidanceStage.value))

const activeRecommendationSeed = computed(() => {
  if (recommendationSeed.value) {
    return recommendationSeed.value
  }
  const draft = form.message.trim()
  if (draft) {
    return draft
  }
  return getLastUserMessage()
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

async function openConversation(id: number) {
  currentConversationId.value = id
  const { data } = await api.get(`/chat/history/${id}`)
  form.subject = data.subject
  guidanceStage.value = data.guidance_stage
  messages.value = data.messages.map((item: { role: 'user' | 'assistant'; content: string }) => ({
    role: item.role,
    content: item.content,
  }))
  const latestUserMessage = getLastUserMessage()
  if (latestUserMessage) {
    void loadRecommendations(latestUserMessage, { silent: true })
  } else {
    resetRecommendations()
  }
}

async function toggleResolved() {
  if (!currentConversationId.value) {
    return
  }
  await api.post(`/chat/${currentConversationId.value}/resolve`, { resolved: true })
  await loadConversations()
  ElMessage.success('已标记为已解决')
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
    if (item.role === 'user' && item.content.trim()) {
      return item.content.trim()
    }
  }
  return ''
}

function resetRecommendations() {
  recommendationPool.value = []
  recommendationOffset.value = 0
  recommendationSeed.value = ''
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

function recommendationMeta(item: QuestionRecommendation): string[] {
  const meta: string[] = [resourceTypeLabel(item.resource_type)]
  if (item.grade) {
    meta.push(`${item.grade}年级`)
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

async function loadRecommendations(seedText: string, options: { silent?: boolean } = {}) {
  const seed = seedText.trim()
  if (!seed) {
    if (!options.silent) {
      ElMessage.info('先输入问题，或先发送一条消息后再推荐练习')
    }
    return
  }

  recommendationLoading.value = true
  recommendationSeed.value = seed
  recommendationError.value = ''
  recommendationOffset.value = 0
  try {
    const data = await fetchQuestionRecommendations({
      subject: form.subject,
      question: seed,
      limit: RECOMMENDATION_FETCH_LIMIT,
    })
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
  void loadRecommendations(activeRecommendationSeed.value)
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
  if (recommendationSeed.value) {
    void loadRecommendations(recommendationSeed.value, { silent: true })
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
  if (!form.message.trim()) {
    return
  }
  const content = form.message.trim()
  form.message = ''
  sending.value = true
  stopRequested = false
  streamAbortController = new AbortController()
  messages.value.push({ role: 'user', content })
  messages.value.push({ role: 'assistant', content: '' })
  queueScrollToBottom()

  try {
    await streamChat(
      {
        subject: form.subject,
        message: content,
        conversation_id: currentConversationId.value,
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
    await loadConversations()
    void loadRecommendations(content, { silent: true })
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
  stopStreaming(false)
})

onMounted(async () => {
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
        <button class="ghost-button" @click="loadConversations">刷新</button>
      </div>
      <div class="conversation-list">
        <button
          v-for="item in conversations"
          :key="item.id"
          class="conversation-card conversation-card--compact"
          @click="openConversation(item.id)"
        >
          <strong class="conversation-card__topic">{{ conversationTopic(item) }}</strong>
          <span class="conversation-card__meta">{{ item.subject }}</span>
          <span>{{ stageLabel(item.guidance_stage) }}</span>
          <span>{{ item.resolved ? '已解决' : '进行中' }}</span>
        </button>
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
          <div class="message-body" v-html="renderMessageBody(item.content)"></div>
        </article>
      </div>

      <div class="chat-controls">
        <el-select v-model="form.subject" :disabled="sending" placeholder="选择学科">
          <el-option v-for="subject in subjects" :key="subject" :label="subject" :value="subject" />
        </el-select>
        <el-input
          v-model="form.message"
          :disabled="sending"
          type="textarea"
          :rows="4"
          resize="none"
          placeholder="输入你的问题，系统会先引导你整理思路"
        />
        <p v-if="sending" class="stream-hint">正在流式生成，可随时停止。</p>
        <div class="chat-actions">
          <button class="primary-button" :disabled="sending" @click="sendMessage">
            {{ sending ? '生成中...' : '发送问题' }}
          </button>
          <button
            class="ghost-button"
            :disabled="sending || recommendationLoading"
            @click="refreshRecommendations"
          >
            {{ recommendationLoading ? '推荐中...' : '推荐同类题' }}
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
              题图会保留在卡片中；带入聊天时当前只会自动带入题干文字，不会直接解析图片内容。
            </p>
          </div>
          <div class="row-actions">
            <button
              class="ghost-button"
              :disabled="recommendationLoading || !activeRecommendationSeed"
              @click="rotateRecommendations"
            >
              换一批
            </button>
          </div>
        </div>

        <p v-if="recommendationSeed" class="recommendation-seed">
          当前推荐依据：{{ recommendationSeed }}
        </p>
        <p v-if="recommendationError" class="recommendation-state recommendation-state--error">
          {{ recommendationError }}
        </p>
        <p v-else-if="recommendationLoading" class="recommendation-state">正在匹配相似练习题...</p>
        <p v-else-if="!hasRecommendations" class="recommendation-state">
          发送问题后会自动推荐同类练习，也可以直接根据当前输入手动推荐。
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
  </section>
</template>
