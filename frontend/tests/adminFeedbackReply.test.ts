import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const adminFeedback = readFileSync(resolve(testDir, '../src/views/AdminFeedback.vue'), 'utf8')

assert.match(
  adminFeedback,
  /const updated = await replyAdminFeedback\(item\.id, \{ reply_content: reply \}\)/,
  'reply save should keep the successful response instead of treating refresh failures as save failures',
)
assert.doesNotMatch(
  adminFeedback,
  /await replyAdminFeedback\(item\.id, \{ reply_content: reply \}\)[\s\S]*?await loadFeedback\(\)[\s\S]*?catch \(error\) \{[\s\S]*?回复保存失败/,
  'reply save failure message should not cover the post-save list refresh',
)
