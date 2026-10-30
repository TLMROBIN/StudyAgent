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
// Preserve readability when the visual palette changes; do not pin an old hex value.
const mutedToken = styles.match(/--sidebar-muted:\s*(#[0-9a-f]{6})\s*;/i)
assert.ok(mutedToken, 'the sidebar should define a dedicated muted color')
assert.match(
  styles,
  /\.app-sidebar \.brand-copy,\s*\.app-sidebar \.profile-chip span\s*\{\s*color:\s*var\(--sidebar-muted\)/,
  'sidebar supporting text should use its dedicated muted color',
)
const sidebarRules = [...styles.matchAll(/(?:^|\n)\.app-sidebar\s*\{([^}]+)\}/g)]
const background = sidebarRules.flatMap((rule) => [...rule[1].matchAll(/(?:^|;)\s*background:\s*(#[0-9a-f]{6})\s*;/gi)]).at(-1)
assert.ok(background, 'the sidebar should have a solid background for contrast validation')
function rgb(hex: string): number[] {
  return [1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16) / 255)
}
function luminance(channels: number[]): number {
  const linear = channels.map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
}
const textLuminance = luminance(rgb(mutedToken[1]))
const sidebarBackground = rgb(background[1])
const profileRules = [...styles.matchAll(/(?:^|\n)\.profile-chip\s*\{([^}]+)\}/g)]
const profileOverlay = profileRules.flatMap((rule) => [...rule[1].matchAll(/background:\s*rgba\(255,\s*255,\s*255,\s*([\d.]+)\)/g)]).at(-1)
assert.ok(profileOverlay, 'the profile chip should use the expected translucent white background')
const opacity = Number(profileOverlay[1])
for (const [label, channels] of [
  ['sidebar', sidebarBackground],
  ['profile chip', sidebarBackground.map((channel) => channel * (1 - opacity) + opacity)],
] as const) {
  const backgroundLuminance = luminance(channels)
  const contrast = (Math.max(textLuminance, backgroundLuminance) + 0.05) / (Math.min(textLuminance, backgroundLuminance) + 0.05)
  assert.ok(contrast >= 4.5, `${label} supporting text should reach 4.5:1 contrast; got ${contrast.toFixed(2)}:1`)
}
assert.match(styles, /\.app-sidebar--collapsed \.sidebar-logout[\s\S]*?white-space:\s*nowrap/, 'compact logout should not wrap vertically')
