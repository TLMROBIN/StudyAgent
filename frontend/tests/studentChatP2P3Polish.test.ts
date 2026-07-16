import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const studentChatSource = readFileSync(resolve(testDir, '../src/views/StudentChat.vue'), 'utf8')
const apiSource = readFileSync(resolve(testDir, '../src/utils/api.ts'), 'utf8')
const appSource = readFileSync(resolve(testDir, '../src/App.vue'), 'utf8')
const styles = readFileSync(resolve(testDir, '../src/styles.css'), 'utf8')

assert.match(apiSource, /fetchChatSubjects[\s\S]*?'\/chat\/subjects'/, 'subject capabilities should come from the backend')
assert.match(studentChatSource, /校本资料与题库可用/, 'subject choices should explain full school-resource support')
assert.match(studentChatSource, /校本资料可用/, 'subject choices should explain school-resource support')
assert.match(studentChatSource, /通用答疑/, 'subject choices should distinguish general tutoring')

assert.match(studentChatSource, /window\.sessionStorage/, 'text drafts should survive route changes and refreshes in the current tab')
assert.match(studentChatSource, /restoreLatestDraftContext/, 'the most recent draft context should be restored after refresh')
assert.match(studentChatSource, /草稿已保留/, 'recoverable send errors should tell students their draft is safe')
assert.doesNotMatch(
  studentChatSource,
  /function chatFailureMessage\([\s\S]*?if \(message\) \{[\s\S]*?return message/,
  'chat failures should never expose arbitrary technical error messages',
)

assert.match(
  studentChatSource,
  /role="log"[\s\S]*?aria-live="off"[\s\S]*?aria-relevant="additions"/,
  'stream chunks should not be announced repeatedly',
)
assert.match(
  studentChatSource,
  /:aria-label="`\$\{deletingConversationIds\.has\(item\.id\)[\s\S]*?\$\{conversationTopic\(item\)\}`"/,
  'history delete actions should include the conversation topic',
)

assert.match(studentChatSource, /aria-label="更多会话操作"/, 'secondary session actions should live behind one More menu')
assert.match(studentChatSource, /v-if="showCompletionAction" command="resolve"/, 'completion should appear only after meaningful progress')
assert.match(studentChatSource, /studentMessageCount\.value >= 2/, 'completion should wait for at least two student turns')
assert.doesNotMatch(
  studentChatSource,
  /class="history-drawer-actions"[\s\S]{0,500}>修改密码</,
  'password changes should not be mixed into learning history actions',
)

assert.doesNotMatch(appSource, /<h1 class="brand-title">/, 'the sidebar brand should not create a second page H1')
assert.match(appSource, /class="ghost-button sidebar-logout" aria-label="退出登录"/, 'compact logout should keep a stable accessible name')
assert.match(styles, /--sidebar-muted:\s*#cbd5e1/, 'the dark sidebar should use a dedicated readable muted token')
assert.match(styles, /\.app-sidebar--collapsed \.sidebar-logout[\s\S]*?white-space:\s*nowrap/, 'compact logout should not wrap vertically')
