import assert from 'node:assert/strict'
import { clearFeedbackDrafts, loadFeedbackDraft, saveFeedbackDraft } from '../src/utils/feedbackDraft.ts'

const values = new Map<string, string>()
const storage = {
  get length() { return values.size },
  key(index: number) { return [...values.keys()][index] ?? null },
  getItem(key: string) { return values.get(key) ?? null },
  setItem(key: string, value: string) { values.set(key, value) },
  removeItem(key: string) { values.delete(key) },
}
Object.defineProperty(globalThis, 'window', { configurable: true, value: { sessionStorage: storage } })
const photo = new File(['synthetic image'], 'example.png', { type: 'image/png' })
saveFeedbackDraft(1, '第一行\n第二行', [photo])
const restored = loadFeedbackDraft(1)
assert.equal(restored.text, '第一行\n第二行')
assert.equal(restored.files[0], photo, 'route changes preserve the selected file without uploading it')
restored.files.length = 0
assert.equal(loadFeedbackDraft(1).files.length, 1, 'a component cannot mutate the cached array')
assert.deepEqual(loadFeedbackDraft(2), { text: '', files: [] }, 'accounts never share drafts')
assert.deepEqual([...values.values()], ['第一行\n第二行'], 'browser storage contains text, never image bytes')
values.set('unrelated-session-setting', 'keep')
clearFeedbackDrafts()
assert.deepEqual(loadFeedbackDraft(1), { text: '', files: [] }, 'logout clears memory and stored text')
assert.equal(values.get('unrelated-session-setting'), 'keep')
values.set('studyagent-feedback-draft:v1:1', 'saved before refresh')
assert.deepEqual(loadFeedbackDraft(1), { text: 'saved before refresh', files: [] }, 'refresh restores text only')
saveFeedbackDraft(1, '', [])
assert.equal(values.has('studyagent-feedback-draft:v1:1'), false, 'successful submission clears the draft')
Object.defineProperty(globalThis, 'window', { configurable: true, value: {
  get sessionStorage() { throw new Error('storage denied') },
} })
saveFeedbackDraft(1, 'memory fallback', [photo])
assert.equal(loadFeedbackDraft(1).files[0], photo)
clearFeedbackDrafts()
assert.deepEqual(loadFeedbackDraft(1), { text: '', files: [] })
