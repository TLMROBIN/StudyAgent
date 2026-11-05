import assert from 'node:assert/strict'
import { rememberConversation, recalledConversation } from '../src/utils/chatSession.ts'

const values = new Map<string, string>()
const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) } }
rememberConversation(storage, 7, 42)
assert.equal(recalledConversation(storage, 7), 42, 'a completed conversation survives page remount')
assert.equal(recalledConversation(storage, 8), null, 'accounts must not share a conversation pointer')
rememberConversation(storage, 7, null)
assert.equal(recalledConversation(storage, 7), null, 'explicit new question must not reopen the old conversation')
for (const value of ['-1', '"42"', '{}', 'broken', '1.5']) {
  values.set('studyagent:last-conversation:v1:7', value)
  assert.equal(recalledConversation(storage, 7), null)
}
const blocked = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
rememberConversation(blocked, 7, 42)
assert.equal(recalledConversation(blocked, 7), null)
