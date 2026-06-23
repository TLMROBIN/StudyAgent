import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const styles = readFileSync(resolve(testDir, '../src/styles.css'), 'utf8')
const mainSource = readFileSync(resolve(testDir, '../src/main.ts'), 'utf8')
const viewportHeightSourcePath = resolve(testDir, '../src/utils/viewportHeight.ts')

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function ruleFor(selector: string) {
  const match = styles.match(new RegExp(`${escapeRegExp(selector)}\\s*\\{([^}]*)\\}`))
  assert.ok(match, `missing ${selector} rule`)
  return match[1].replace(/\s+/g, ' ')
}

const bubbleRule = ruleFor('.bubble')
assert.match(bubbleRule, /flex:\s*0 0 auto/, 'chat bubbles should not collapse under flex sizing')
assert.match(bubbleRule, /width:\s*auto/, 'chat bubbles should use stable intrinsic sizing')
assert.doesNotMatch(
  bubbleRule,
  /width:\s*fit-content/,
  'fit-content creates unstable right-aligned bubble widths in Chrome on narrow tablets',
)
assert.doesNotMatch(
  bubbleRule,
  /(?:^|;\s*)width:\s*min\(78%,\s*720px\)/,
  'chat bubbles should not use a fixed half-panel width',
)
assert.match(bubbleRule, /max-width:\s*min\(78%,\s*720px\)/, 'long chat bubbles should wrap at the readable width cap')
assert.match(bubbleRule, /overflow-wrap:\s*anywhere/, 'long tokens should wrap before they push a bubble off screen')

const userBubbleRule = ruleFor('.bubble.user')
assert.match(userBubbleRule, /align-self:\s*flex-end/, 'student bubbles should stay right aligned')

const messageBodyRule = ruleFor('.message-body')
assert.match(messageBodyRule, /max-width:\s*100%/, 'message bodies should stay inside the bubble width cap')
assert.doesNotMatch(
  messageBodyRule,
  /(?:^|;\s*)width:\s*100%/,
  'message bodies should not force a cyclic full-width calculation inside content-sized bubbles',
)

for (const selector of ['.student-history-panel .conversation-list', '.chat-stream']) {
  const rule = ruleFor(selector)
  assert.match(rule, /-webkit-overflow-scrolling:\s*touch/, `${selector} should support momentum touch scrolling`)
  assert.match(rule, /overscroll-behavior:\s*contain/, `${selector} should keep scroll gestures inside the panel`)
  assert.match(rule, /touch-action:\s*pan-y/, `${selector} should allow vertical touch dragging`)
}

for (const selector of ['.student-history-panel .conversation-list', '.chat-stream']) {
  assert.ok(styles.includes(`${selector}::-webkit-scrollbar`), `${selector} needs a visible scrollbar track in Chromium/WebKit`)
}

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\)[\s\S]*?\.student-page-grid\s*\{[\s\S]*?grid-template-columns:\s*minmax\(126px,\s*144px\) minmax\(0,\s*1fr\)/,
  'student page should stay two-column on tablet widths',
)

assert.match(
  styles,
  /--student-panel-height:\s*calc\(var\(--app-viewport-height,\s*100vh\) - var\(--shell-main-block-padding\)\)/,
  'student panels should use the measured visual viewport instead of raw 100vh',
)

for (const selector of ['.student-page-grid', '.student-history-panel', '.chat-panel']) {
  const rule = ruleFor(selector)
  assert.match(rule, /var\(--student-panel-height\)/, `${selector} should track the measured student panel height`)
  assert.doesNotMatch(rule, /calc\(100vh - 56px\)/, `${selector} should not use raw 100vh tablet height`)
}

const chatPanelRule = ruleFor('.chat-panel')
assert.match(
  chatPanelRule,
  /grid-template-columns:\s*minmax\(0,\s*1fr\)/,
  'chat panel should use a shrinkable grid column so long notice text cannot widen the chat stream',
)

const chatPanelChildrenRule = ruleFor('.chat-panel > *')
assert.match(
  chatPanelChildrenRule,
  /min-width:\s*0/,
  'chat panel grid children should be allowed to shrink inside the visible panel boundary',
)

const studentChatSource = readFileSync(resolve(testDir, '../src/views/StudentChat.vue'), 'utf8')
assert.match(
  studentChatSource,
  /class="chat-subject-select"/,
  'student subject selector should have a dedicated width class',
)

const chatSubjectSelectRule = ruleFor('.chat-subject-select')
assert.match(chatSubjectSelectRule, /flex:\s*0 0 108px/, 'subject selector should reserve readable label width')
assert.match(chatSubjectSelectRule, /min-width:\s*108px/, 'subject selector should not collapse to an icon-only control')

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\)[\s\S]*?\.student-page-grid \.chat-panel\s*\{[\s\S]*?grid-template-rows:\s*auto auto minmax\(72px,\s*1fr\) auto auto/,
  'tablet chat stream should be allowed to shrink so the composer stays visible',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\)[\s\S]*?\.student-page-grid \.chat-actions\s*\{[\s\S]*?order:\s*2/,
  'tablet action buttons should be prioritized before secondary helper copy',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\)[\s\S]*?\.student-page-grid \.panel-subcopy\s*\{[\s\S]*?order:\s*3/,
  'tablet helper copy should not push send controls below the panel',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1280px\) and \(orientation:\s*landscape\)[\s\S]*?\.student-page-grid \.chat-actions\s*\{[\s\S]*?order:\s*2/,
  'wide legacy landscape tablets should keep chat action buttons before helper copy',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1280px\) and \(orientation:\s*landscape\)[\s\S]*?\.student-page-grid \.panel-subcopy\s*\{[\s\S]*?order:\s*3/,
  'wide legacy landscape helper copy should be lower priority than action buttons',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\) and \(max-height:\s*620px\)[\s\S]*?:root\s*\{[\s\S]*?--shell-main-block-padding:\s*24px/,
  'low-height landscape tablets should reduce the shell vertical budget so chat actions stay visible',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\) and \(max-height:\s*620px\)[\s\S]*?\.app-root--shell \.app-main\s*\{[\s\S]*?padding-top:\s*12px[\s\S]*?padding-bottom:\s*12px/,
  'low-height landscape tablets should use tighter page padding',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\) and \(max-height:\s*620px\)[\s\S]*?\.student-page-grid \.chat-panel\s*\{[\s\S]*?gap:\s*10px[\s\S]*?padding:\s*14px[\s\S]*?grid-template-rows:\s*auto auto minmax\(40px,\s*1fr\) auto auto/,
  'low-height landscape chat panels should compact fixed chrome before clipping action buttons',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\) and \(max-height:\s*620px\)[\s\S]*?\.chat-controls \.el-textarea__inner\s*\{[\s\S]*?height:\s*62px/,
  'low-height landscape composer should use a shorter text area',
)

assert.match(
  styles,
  /@media \(min-width:\s*641px\) and \(max-width:\s*1080px\) and \(max-height:\s*620px\)[\s\S]*?\.chat-actions \.ghost-button,[\s\S]*?\.chat-actions \.primary-button\s*\{[\s\S]*?padding:\s*9px 14px/,
  'low-height landscape action buttons should use compact padding',
)

assert.match(mainSource, /installViewportHeight/, 'main.ts should install the visual viewport height synchronizer')
assert.ok(
  mainSource.indexOf('installViewportHeight()') > -1 &&
    mainSource.indexOf('installViewportHeight()') < mainSource.indexOf("app.mount('#app')"),
  'visual viewport height should be synced before mounting the app',
)

const viewportHeightSource = readFileSync(viewportHeightSourcePath, 'utf8')
assert.match(viewportHeightSource, /visualViewport/, 'viewport helper should prefer visualViewport on tablet WebViews')
assert.match(viewportHeightSource, /innerHeight/, 'viewport helper should fall back to innerHeight')
assert.match(
  viewportHeightSource,
  /setProperty\(\s*VIEWPORT_HEIGHT_PROPERTY/,
  'viewport helper should write the measured height to the root CSS variable',
)
