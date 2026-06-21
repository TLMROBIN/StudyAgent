import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const styles = readFileSync(resolve(testDir, '../src/styles.css'), 'utf8')

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function ruleFor(selector: string) {
  const match = styles.match(new RegExp(`${escapeRegExp(selector)}\\s*\\{([^}]*)\\}`))
  assert.ok(match, `missing ${selector} rule`)
  return match[1].replace(/\s+/g, ' ')
}

const bubbleRule = ruleFor('.bubble')
assert.ok(!/width:\s*fit-content\b/.test(bubbleRule), 'student bubbles must not shrink to min-content width')
assert.match(bubbleRule, /width:\s*min\(78%,\s*720px\)/, 'bubbles should have a stable readable width capped by the chat stream')
assert.match(bubbleRule, /max-width:\s*100%/, 'bubbles must not overflow their chat stream')

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
