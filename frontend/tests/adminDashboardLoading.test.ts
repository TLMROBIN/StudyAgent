import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'

const source = readFileSync(new URL('../src/views/AdminDashboard.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)![1]
const dir = await mkdtemp(join(tmpdir(), 'studyagent-dashboard-test-'))
const requests: Array<{ path: string; signal: AbortSignal; resolve: (data: unknown) => void; reject: (error: Error) => void }> = []
const request = (path: string, options: any = {}) => new Promise((resolve, reject) => {
  requests.push({ path, signal: options.signal, resolve: (data) => resolve({ data }), reject })
})
const lifecycle: Array<() => void> = []
;(globalThis as any).__dashboardTest = {
  api: { get: request },
  fetchTeacherIncentivePortraits: async (_id: unknown, signal: AbortSignal) => (await request('/incentive/teacher/portraits', { signal }) as any).data,
  onBeforeUnmount: (callback: () => void) => lifecycle.push(callback),
}
try {
  await build({
    stdin: { contents: script + '\nexport { loadDashboard, loadOverview, loadTrend, overview, portraits, sectionStates, trend, trendDateRange, trendLoading, trendError };', resolveDir: resolve('frontend/src/views'), loader: 'ts' },
    outfile: join(dir, 'dashboard.mjs'), bundle: true, format: 'esm', platform: 'node',
    plugins: [{ name: 'dashboard-test-dependencies', setup(builder) {
      builder.onResolve({ filter: /^(vue|element-plus)$|\.\.\/utils\/|\.vue$/ }, (args) => ({ path: args.path, namespace: 'mock' }))
      builder.onLoad({ filter: /.*/, namespace: 'mock' }, (args) => {
        if (args.path === 'vue') return { contents: `export { ref, reactive, computed, watch } from ${JSON.stringify(resolve('frontend/node_modules/vue/dist/vue.runtime.esm-bundler.js'))}; export const onMounted = () => {}; export const onBeforeUnmount = globalThis.__dashboardTest.onBeforeUnmount;`, resolveDir: resolve('frontend') }
        if (args.path === 'element-plus') return { contents: 'export const ElMessage = {}; export const ElMessageBox = {};' }
        if (args.path.endsWith('/api')) return { contents: 'export const { api, fetchTeacherIncentivePortraits } = globalThis.__dashboardTest; export const archiveAdminNotification = () => {}; export const createAdminNotification = () => {}; export const fetchAdminNotifications = () => {}; export const fetchStudentReflections = () => {}; export const praiseStudent = () => {}; export const updateAdminNotification = () => {};' }
        if (args.path.endsWith('/richText')) return { contents: 'export const renderRichText = (s) => s;' }
        if (args.path.endsWith('/subjectPieChart')) return { contents: 'export const buildSubjectPieModel = () => ({});' }
        return { contents: 'export default {};' }
      })
    } }],
  })
  const state = await import(pathToFileURL(join(dir, 'dashboard.mjs')).href)
  const loading = state.loadDashboard()
  assert.equal(requests.length, 4, 'incentive data starts alongside stats, before the slow endpoints finish')
  requests.find((r) => r.path === '/stats/overview')!.resolve({ total_questions: 8, resolved_rate: 0.5, average_turns: 2, by_subject: [] })
  await new Promise((done) => setImmediate(done))
  assert.equal(state.overview.value.total_questions, 8, 'fast overview is visible while other requests remain pending')
  assert.equal(state.sectionStates.overview.loaded, true)
  requests.find((r) => r.path === '/stats/classes')!.reject(new Error('504'))
  requests.find((r) => r.path === '/stats/portraits')!.resolve([{ student_id: 7 }])
  requests.find((r) => r.path === '/incentive/teacher/portraits')!.resolve([])
  await loading
  assert.equal(state.sectionStates.classes.loaded, false, 'failure is not represented as an empty successful dataset')
  assert.ok(state.sectionStates.classes.error)
  assert.equal(state.portraits.value.length, 1, 'one section failure must not discard other results')

  const first = state.loadOverview()
  const oldRequest = requests.at(-1)!
  const second = state.loadOverview()
  const latestRequest = requests.at(-1)!
  assert.equal(oldRequest.signal.aborted, true)
  latestRequest.resolve({ total_questions: 10 }); await second
  oldRequest.resolve({ total_questions: 1 }); await first
  assert.equal(state.overview.value.total_questions, 10, 'late aborted responses cannot replace fresh data')

  const oldTrend = state.loadTrend()
  const oldTrendRequest = requests.at(-1)!
  state.trendDateRange.value = ['2026-05-01', '2026-05-02']
  await new Promise((done) => setImmediate(done))
  assert.equal(oldTrendRequest.signal.aborted, true, 'changing filters cancels the previous request immediately')
  const count = requests.length
  state.trendDateRange.value = ['2026-05-01', '2026-05-03']
  await new Promise((done) => setTimeout(done, 300))
  assert.equal(requests.length, count + 1, 'rapid filter changes produce one debounced request')
  const freshTrend = requests.at(-1)!
  freshTrend.resolve({ labels: ['fresh'], series: [], available_subjects: [] })
  await new Promise((done) => setImmediate(done))
  oldTrendRequest.resolve({ labels: ['stale'], series: [], available_subjects: [] }); await oldTrend
  assert.deepEqual([...state.trend.value.labels], ['fresh'])
  assert.equal(state.trendLoading.value, false)
  assert.equal(state.trendError.value, '')

  void state.loadTrend()
  const unmountedRequest = requests.at(-1)!
  lifecycle.forEach((callback) => callback())
  assert.equal(unmountedRequest.signal.aborted, true, 'leaving the dashboard cancels pending requests')
  unmountedRequest.reject(new Error('cancelled'))
  await new Promise((done) => setImmediate(done))
  console.log('dashboard independent results / failure states / cancellation / debounce / stale responses: passed')
} finally {
  await rm(dir, { recursive: true, force: true })
  delete (globalThis as any).__dashboardTest
}
