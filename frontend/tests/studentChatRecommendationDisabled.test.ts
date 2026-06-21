import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const studentChat = readFileSync(resolve(testDir, '../src/views/StudentChat.vue'), 'utf8')

assert.match(
  studentChat,
  /const SHOW_RECOMMENDATION_PANEL = false/,
  'student recommendation module should be disabled by default while practice moves into chat',
)

assert.match(
  studentChat,
  /<section v-if="SHOW_RECOMMENDATION_PANEL" class="recommendation-panel">/,
  'the standalone recommendation panel should be hidden behind an explicit reversible flag',
)
