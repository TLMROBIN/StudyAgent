import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const apiSource = readFileSync(resolve(testDir, '../src/utils/api.ts'), 'utf8')
const studentChat = readFileSync(resolve(testDir, '../src/views/StudentChat.vue'), 'utf8')

assert.match(
  apiSource,
  /export interface ChatMessageRead \{[\s\S]*?assets\?: KnowledgeAsset\[\]/,
  'chat messages should expose optional knowledge assets for inline question images',
)

assert.match(
  studentChat,
  /last\.assets = data\.assets as KnowledgeAsset\[\]/,
  'SSE done event should attach returned practice-question assets to the assistant bubble',
)

assert.match(
  studentChat,
  /item\.assets \|\| \[\]\)/,
  'chat bubble rendering should pass message assets to rich-text rendering',
)

assert.match(
  studentChat,
  /items\.flatMap\(\(item\) => item\.assets \|\| \[\]\)/,
  'history preload should include message-level knowledge assets',
)

assert.match(
  studentChat,
  /assets: item\.assets \|\| \[\]/,
  'loaded conversation history should preserve message-level assets for later rendering',
)
