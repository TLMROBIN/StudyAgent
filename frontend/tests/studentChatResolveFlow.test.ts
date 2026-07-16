import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const studentChatSource = readFileSync(resolve(testDir, '../src/views/StudentChat.vue'), 'utf8')
const apiSource = readFileSync(resolve(testDir, '../src/utils/api.ts'), 'utf8')

assert.doesNotMatch(
  studentChatSource,
  /ElMessageBox\.prompt\([\s\S]*?完成本次思考/,
  'completion should not use a two-outcome prompt that treats dismissal as success',
)

for (const label of ['取消', '跳过反思并完成', '提交反思并完成']) {
  assert.match(studentChatSource, new RegExp(label), `completion dialog should expose the explicit action: ${label}`)
}

assert.match(
  studentChatSource,
  /function resetResolveDialog\(\)[\s\S]*?resolveDialogVisible\.value = false[\s\S]*?resolveReflection\.value = ''/,
  'closing or cancelling should discard the dialog state without completing the conversation',
)
assert.match(
  studentChatSource,
  /async function completeConversation\(skipReflection: boolean\)[\s\S]*?reflection = skipReflection \? null : resolveReflection\.value\.trim\(\)[\s\S]*?resolved: true/,
  'completion should distinguish an explicit skip from a submitted reflection',
)
assert.match(
  studentChatSource,
  /async function restoreConversation\(\)[\s\S]*?resolved: false[\s\S]*?已恢复为继续思考/,
  'students should be able to restore a completed conversation to active thinking',
)
assert.match(
  studentChatSource,
  /currentConversationResolved \? '恢复继续思考' : '完成本次思考'/,
  'the session action should communicate the reversible state directly',
)
assert.match(
  studentChatSource,
  /:close-on-click-modal="false"[\s\S]*?:close-on-press-escape="!resolveSubmitting"[\s\S]*?@closed="resetResolveDialog"/,
  'the completion dialog should preserve input while submitting and treat ordinary closure as cancellation',
)

assert.match(
  apiSource,
  /export interface ChatConversationRead \{[\s\S]*?resolved: boolean[\s\S]*?incentive\?: IncentiveGrant \| null/,
  'conversation responses should expose the authoritative resolved state and incentive result',
)
