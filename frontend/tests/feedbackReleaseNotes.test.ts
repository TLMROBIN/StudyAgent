import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const apiSource = readFileSync(resolve(testDir, '../src/utils/api.ts'), 'utf8')
const appSource = readFileSync(resolve(testDir, '../src/App.vue'), 'utf8')
const studentFeedback = readFileSync(resolve(testDir, '../src/views/StudentFeedback.vue'), 'utf8')
const adminFeedback = readFileSync(resolve(testDir, '../src/views/AdminFeedback.vue'), 'utf8')

assert.match(apiSource, /export interface FeedbackUnreadSummary/, 'API should expose unread feedback/release-note summary')
assert.match(apiSource, /fetchFeedbackUnreadSummary/, 'student shell should be able to load unread summary')
assert.match(apiSource, /fetchReleaseNotes/, 'student page should be able to load release notes')
assert.match(apiSource, /markReleaseNoteRead/, 'student page should mark release notes as read')
assert.match(apiSource, /createAdminReleaseNote/, 'admin page should publish release notes')

assert.match(appSource, /fetchFeedbackUnreadSummary/, 'student sidebar should fetch unread summary')
assert.match(appSource, /showUnreadDot/, 'navigation items should carry unread red-dot state')
assert.match(appSource, /nav-unread-dot/, 'sidebar should render an unread red dot')

assert.match(studentFeedback, /activeSection/, 'feedback page should switch between feedback and release-note sections')
assert.match(studentFeedback, /更新日志/, 'feedback page should include a release-note entry')
assert.match(studentFeedback, /unread-release-dot/, 'release-note tab should show unread red dot')
assert.match(studentFeedback, /markFeedbackRead/, 'feedback replies should be marked read')
assert.match(studentFeedback, /markReleaseNoteRead/, 'release notes should be marked read')

assert.match(adminFeedback, /releaseNoteForm/, 'admin feedback page should include a release-note publishing form')
assert.match(adminFeedback, /发布更新日志/, 'admin page should expose release-note publishing copy')
assert.match(adminFeedback, /createAdminReleaseNote/, 'admin page should call the admin release-note API')
