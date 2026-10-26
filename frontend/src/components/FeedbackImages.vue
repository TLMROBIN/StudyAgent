<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElImageViewer, ElMessage } from 'element-plus'
import 'element-plus/es/components/image-viewer/style/css'

import { useAuthorizedAssets } from '../composables/useAuthorizedAssets'
import type { FeedbackAttachment } from '../utils/api'

const props = defineProps<{ attachments: FeedbackAttachment[] }>()
const { assetUrl, ensureAssetUrl } = useAuthorizedAssets()
const failed = ref<Record<string, boolean>>({})
const opening = ref<string | null>(null)
const previewUrl = ref('')
let openRequest = 0

async function load(attachment: FeedbackAttachment) {
  failed.value[attachment.attachment_id] = false
  try {
    return await ensureAssetUrl(attachment)
  } catch (error) {
    failed.value[attachment.attachment_id] = true
    throw error
  }
}

watch(() => props.attachments, (attachments) => {
  void Promise.allSettled(attachments.map(load))
}, { immediate: true })

async function preview(attachment: FeedbackAttachment) {
  const request = ++openRequest
  opening.value = attachment.attachment_id
  try {
    const url = await load(attachment)
    if (request === openRequest) previewUrl.value = url
  } catch {
    if (request === openRequest) ElMessage.error('图片加载失败，请点击图片重试')
  } finally {
    if (request === openRequest) opening.value = null
  }
}
</script>

<template>
  <div v-if="attachments.length" class="feedback-image-grid">
    <button
      v-for="attachment in attachments"
      :key="attachment.attachment_id"
      type="button"
      class="feedback-image-thumb"
      :aria-label="`查看图片：${attachment.filename}`"
      :aria-busy="opening === attachment.attachment_id"
      @click="preview(attachment)"
    >
      <span v-if="failed[attachment.attachment_id]">图片加载失败，点击重试</span>
      <img v-else-if="assetUrl(attachment)" :src="assetUrl(attachment)" :alt="attachment.filename" loading="lazy" />
      <span v-else>图片加载中...</span>
    </button>
  </div>
  <ElImageViewer
    v-if="previewUrl"
    :url-list="[previewUrl]"
    teleported
    hide-on-click-modal
    @close="previewUrl = ''"
  />
</template>

<style scoped>
.feedback-image-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.feedback-image-thumb {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 112px;
  height: 112px;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface-color, #f5f7f8);
  cursor: pointer;
}

.feedback-image-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.feedback-image-thumb span {
  padding: 8px;
  color: var(--muted);
  font-size: 12px;
}
</style>
