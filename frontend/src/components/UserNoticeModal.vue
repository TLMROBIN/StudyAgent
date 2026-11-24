<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onBeforeUnmount, ref, watch } from 'vue'

import { useAuthStore } from '../stores/auth'

const COUNTDOWN_SECONDS = 10

const props = defineProps<{
  visible: boolean
  version: string
}>()

const emit = defineEmits<{
  decline: []
}>()

const auth = useAuthStore()
const remainingSeconds = ref(COUNTDOWN_SECONDS)
const submitting = ref(false)
const declining = ref(false)
let countdownTimer: number | null = null

function clearCountdownTimer() {
  if (countdownTimer !== null) {
    window.clearInterval(countdownTimer)
    countdownTimer = null
  }
}

function startCountdown() {
  clearCountdownTimer()
  remainingSeconds.value = COUNTDOWN_SECONDS
  countdownTimer = window.setInterval(() => {
    if (remainingSeconds.value <= 1) {
      remainingSeconds.value = 0
      clearCountdownTimer()
      return
    }
    remainingSeconds.value -= 1
  }, 1000)
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      submitting.value = false
      declining.value = false
      startCountdown()
    } else {
      clearCountdownTimer()
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  clearCountdownTimer()
})

async function handleAccept() {
  if (remainingSeconds.value > 0 || submitting.value || declining.value) {
    return
  }
  submitting.value = true
  try {
    await auth.acceptUserNotice(props.version)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '提交确认失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

function handleDecline() {
  if (submitting.value || declining.value) {
    return
  }
  declining.value = true
  emit('decline')
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="首次使用须知"
    width="min(680px, 94vw)"
    class="user-notice-dialog"
    append-to-body
    align-center
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
  >
    <p class="user-notice-dialog__subtitle">请认真阅读以下说明，阅读满 10 秒后即可确认进入</p>
    <div class="user-notice-dialog__body">
      <section class="user-notice-section">
        <h3>为什么这里不直接给答案</h3>
        <p>这套系统采用引导式答疑，只给思路提示，把推导和作答留给你自己。这样做有明确的实证依据。</p>
        <p>
          宾夕法尼亚大学沃顿商学院团队在《美国国家科学院院刊》（PNAS，2025）发表过一项面向近千名高中生的随机对照实验。练习阶段使用直接给答案的普通 AI 时，学生当场正确率高出 48%，可一旦撤掉 AI 独立考试，成绩反比完全不用 AI 的同学低 17%。同一实验中，改用“只给提示、不给答案”的引导式 AI 后，独立考试的退步就消失了。
        </p>
        <p>
          麻省理工学院媒体实验室（MIT Media Lab）在 2025 年的脑电研究《Your Brain on ChatGPT》中也观察到类似现象。长期让大模型直接代劳思考和写作，受试者大脑神经网络连接活跃度明显下降，研究者把这种透支称作“认知负债”。
        </p>
        <p>
          抄完现成答案时的轻松感，往往是以牺牲自己的思考能力换来的。这里只帮你迈开思路，最后那一步必须由你自己想明白、写出来，这样才能真正学到东西。
        </p>
        <p class="user-notice-citation">
          参考研究（Bastani et al., PNAS 2025, "Generative AI without guardrails can harm learning"; Kosmyna et al., MIT Media Lab 2025, "Your Brain on ChatGPT"）
        </p>
      </section>

      <section class="user-notice-section user-notice-section--warning">
        <h3>个人开发与零容忍红线</h3>
        <p>本平台是老师个人利用业余时间开发的教学辅助工具，完全为爱发电，没有任何商业团队运维。</p>
        <p>
          为了不把有限的时间精力耗在无关琐事上，也为了不影响继续维护的心情，平台对任何滥用行为一律零容忍。凡是拿来闲聊灌水、拿无关内容刷屏，或者刻意套话绕开限制的，一经发现，直接封号到毕业。
        </p>
      </section>

      <section class="user-notice-section">
        <h3>关于正常提问被误判的说明</h3>
        <p>
          如果你提交的正常学科问题被系统误判为非高中物理知识或遭到拦截，请多包涵。这是因为此前有同学滥用平台，为了守住使用秩序，系统不得不把审核标准调得更严。
        </p>
        <p>
          接下来我会持续调试并训练识别模型，让正常提问的识别越来越精准。如果你遇到误判，欢迎在“意见反馈”里附上原题内容，这能直接帮我把识别调准。
        </p>
      </section>

      <section class="user-notice-section user-notice-section--caution">
        <h3>学科范围与重要提醒</h3>
        <p>目前本平台只有物理配备了专门的校本知识库与题库，今后打磨答疑能力的重点也会一直放在物理上。</p>
        <p>其他学科目前仅仅处于能用状态，对答疑质量不做任何保证。</p>
        <p>
          最后请务必记住，AI 是会犯错的。不论物理还是其他学科，系统给出的提示、公式或推导都有可能出错。请始终保持独立判断，结合教材和课堂笔记自行核对，遇到拿不准的地方及时向任课老师求证。
        </p>
      </section>
    </div>

    <template #footer>
      <div class="user-notice-dialog__footer">
        <button
          type="button"
          class="ghost-button"
          :disabled="submitting || declining"
          @click="handleDecline"
        >
          不同意并退出
        </button>
        <button
          type="button"
          class="primary-button"
          :disabled="remainingSeconds > 0 || submitting || declining"
          @click="handleAccept"
        >
          <template v-if="submitting">提交中...</template>
          <template v-else-if="remainingSeconds > 0">我已阅读并同意（{{ remainingSeconds }}s）</template>
          <template v-else>我已阅读并同意</template>
        </button>
      </div>
    </template>
  </el-dialog>
</template>
