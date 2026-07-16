import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const styles = readFileSync(resolve(testDir, '../src/styles.css'), 'utf8')
const mainSource = readFileSync(resolve(testDir, '../src/main.ts'), 'utf8')
const appSource = readFileSync(resolve(testDir, '../src/App.vue'), 'utf8')
const studentChatSource = readFileSync(resolve(testDir, '../src/views/StudentChat.vue'), 'utf8')
const viewportHeightSource = readFileSync(resolve(testDir, '../src/utils/viewportHeight.ts'), 'utf8')

function escapeRegExp(value: string) {
  return value.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&')
}

function ruleFor(selector: string) {
  const match = styles.match(new RegExp(escapeRegExp(selector) + '\\s*\\{([^}]*)\\}'))
  assert.ok(match, 'missing ' + selector + ' rule')
  return match[1].replace(/\s+/g, ' ')
}

const studentPageRule = ruleFor('.student-page-grid')
assert.match(studentPageRule, /display:\s*block/, 'student chat should be a single task surface instead of a permanent split view')
assert.match(studentPageRule, /height:\s*var\(--student-panel-height\)/, 'student task mode should track the measured viewport height')
assert.match(studentPageRule, /overflow:\s*hidden/, 'student task mode should keep page chrome from creating a second scrollbar')

const chatPanelRule = ruleFor('.chat-panel')
assert.match(chatPanelRule, /display:\s*flex/, 'chat surface should use a resilient vertical task flow')
assert.match(chatPanelRule, /flex-direction:\s*column/, 'chat surface should stack variable settings and notices without implicit grid rows')
assert.match(chatPanelRule, /height:\s*var\(--student-panel-height\)/, 'chat surface should fill the measured study viewport')
assert.match(chatPanelRule, /overflow:\s*hidden/, 'chat stream should own scrolling inside the study surface')
assert.match(chatPanelRule, /border-radius:\s*16px/, 'student surface should avoid the previous over-rounded 28px panel treatment')
assert.doesNotMatch(chatPanelRule, /box-shadow:\s*var\(--shadow\)/, 'student surface should not reuse the broad ghost-card shadow')

const messageRowRule = ruleFor('.message-row')
assert.match(messageRowRule, /flex:\s*0 0 auto/, 'message rows should not collapse under flex sizing')
assert.match(messageRowRule, /width:\s*auto/, 'message rows should use stable intrinsic sizing')
assert.match(messageRowRule, /max-width:\s*min\(78%,\s*720px\)/, 'long messages should wrap at the readable width cap')
assert.match(messageRowRule, /min-width:\s*0/, 'message rows should be allowed to shrink inside the chat stream')

const bubbleRule = ruleFor('.bubble')
assert.match(bubbleRule, /width:\s*100%/, 'chat bubbles should fill their message row')
assert.match(bubbleRule, /min-width:\s*0/, 'chat bubbles should stay inside their message row')
assert.match(bubbleRule, /overflow-wrap:\s*anywhere/, 'long tokens should wrap before they push a bubble off screen')
assert.match(bubbleRule, /box-sizing:\s*border-box/, 'bubble padding should stay inside the message row width')

const messageBodyRule = ruleFor('.message-body')
assert.match(messageBodyRule, /max-width:\s*100%/, 'message bodies should stay inside the bubble width cap')
assert.doesNotMatch(
  messageBodyRule,
  /(?:^|;\s*)width:\s*100%/,
  'message bodies should not force a cyclic full-width calculation inside content-sized bubbles',
)

for (const selector of ['.history-drawer-content .conversation-list', '.chat-stream']) {
  const rule = ruleFor(selector)
  assert.match(rule, /-webkit-overflow-scrolling:\s*touch/, selector + ' should support momentum touch scrolling')
  assert.match(rule, /overscroll-behavior:\s*contain/, selector + ' should keep scroll gestures inside the surface')
  assert.match(rule, /touch-action:\s*pan-y/, selector + ' should allow vertical touch dragging')
}

const suggestedReplyRule = ruleFor('.suggested-reply-chip')
assert.match(suggestedReplyRule, /min-height:\s*44px/, 'suggested replies should meet the touch target baseline')

const cropHandleRule = ruleFor('.image-cropper__handle')
assert.match(cropHandleRule, /width:\s*44px/, 'crop resize handle should expose a 44px touch target')
assert.match(cropHandleRule, /height:\s*44px/, 'crop resize handle should expose a 44px touch target')

assert.match(
  studentChatSource,
  /<el-drawer[\s\S]*?v-model="historyOpen"[\s\S]*?class="study-history-drawer"/,
  'conversation history should be an on-demand drawer',
)
assert.doesNotMatch(
  studentChatSource,
  /<aside class="panel panel-tight student-history-panel">/,
  'conversation history should not permanently consume the study viewport',
)
assert.match(studentChatSource, /class="study-session-header"/, 'student task mode should have a focused session header')
assert.match(studentChatSource, /class="session-subject-select"/, 'subject should stay visible near the session title')
assert.match(studentChatSource, /subject:\s*'物理'/, 'the first session should default to the only subject with a complete knowledge base')
assert.doesNotMatch(studentChatSource, /class="chat-model-select"/, 'model selection should not be exposed in the primary student flow')
assert.match(
  studentChatSource,
  /v-if="settingsOpen"[\s\S]*?id="study-session-settings"/,
  'optional guidance settings should use progressive disclosure',
)
assert.match(studentChatSource, /return replies\.slice\(0, 3\)/, 'suggested replies should stay within the working-memory limit')
assert.match(studentChatSource, /class="chat-empty-state"/, 'first use should provide a meaningful empty state')
assert.match(studentChatSource, /STARTER_PROMPTS/, 'first use should provide concrete starting prompts')
assert.match(studentChatSource, /role="log"[\s\S]*?aria-live="off"/, 'chat chunks should not trigger repeated live announcements')
assert.match(
  studentChatSource,
  /class="sr-only" aria-live="polite" aria-atomic="true">\{\{ screenReaderAnnouncement \}\}/,
  'streaming state and final replies should use one atomic live region',
)
assert.doesNotMatch(studentChatSource, /Student Workspace/, 'student surface should not use generic English eyebrow copy')
assert.doesNotMatch(studentChatSource, /class="student-notice-bar"/, 'school notices should not use the old marquee container')
assert.doesNotMatch(styles, /notice-marquee/, 'school notices should not animate continuously')

assert.match(appSource, /window\.matchMedia\('\(max-width: 1280px\)'\)/, 'student task mode should compact the global shell on tablets')
assert.match(appSource, /app-root--student-chat/, 'the app shell should expose a student task-mode styling hook')
assert.match(
  appSource,
  /sidebarIsCollapsed = computed\(\(\) => sidebarCollapsed\.value \|\| forceCompactStudentShell\.value\)/,
  'tablet task mode should default the global sidebar to its compact state',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\)[\s\S]*?\.study-session-header\s*\{[\s\S]*?flex-direction:\s*column/,
  'tablet task header should reflow instead of compressing the chat column',
)
assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\) and \(max-height:\s*620px\)[\s\S]*?\.chat-controls \.chat-message-input \.el-textarea__inner\s*\{[\s\S]*?height:\s*56px/,
  'low-height landscape tablets should compact the composer without hiding it',
)
assert.match(
  styles,
  /@media \(prefers-reduced-motion:\s*reduce\)/,
  'student experience should respect the system reduced-motion preference',
)
assert.match(
  styles,
  /@media \(pointer:\s*coarse\)[\s\S]*?min-height:\s*44px/,
  'touch devices should enforce the 44px interaction baseline',
)

assert.match(
  styles,
  /--student-panel-height:\s*calc\(var\(--app-viewport-height,\s*100vh\) - var\(--shell-main-block-padding\)\)/,
  'student panels should use the measured visual viewport instead of raw 100vh',
)
assert.match(mainSource, /installViewportHeight/, 'main.ts should install the visual viewport height synchronizer')
assert.ok(
  mainSource.indexOf('installViewportHeight()') > -1
    && mainSource.indexOf('installViewportHeight()') < mainSource.indexOf("app.mount('#app')"),
  'visual viewport height should be synced before mounting the app',
)
assert.match(viewportHeightSource, /visualViewport/, 'viewport helper should prefer visualViewport on tablet WebViews')
assert.match(viewportHeightSource, /innerHeight/, 'viewport helper should fall back to innerHeight')
assert.match(
  viewportHeightSource,
  /setProperty\(\s*VIEWPORT_HEIGHT_PROPERTY/,
  'viewport helper should write the measured height to the root CSS variable',
)
