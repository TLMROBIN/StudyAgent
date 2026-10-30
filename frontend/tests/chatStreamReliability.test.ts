import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const dir = await mkdtemp(join(tmpdir(), 'studyagent-stream-test-'))
try {
  await build({ entryPoints: [new URL('../src/utils/api.ts', import.meta.url).pathname], outfile: join(dir, 'api.mjs'), bundle: true, format: 'esm', platform: 'browser', define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', VITE_API_BASE_URL: '/api' }) } })
  globalThis.window = { setTimeout, clearTimeout, localStorage: { getItem: () => '' }, location: { origin: 'http://test.local' } } as any
  const { streamChat } = await import(pathToFileURL(join(dir, 'api.mjs')).href)
  const payload = { subject: '数学', message: '函数如何判断' }
  let calls = 0
  const failure = '模型调用失败，请联系管理员老师或稍后再试。'
  globalThis.fetch = async () => {
    calls++
    return new Response(`event: error\ndata: ${JSON.stringify({ message: failure })}\n\n`)
  }
  await assert.rejects(streamChat(payload, () => {}, { retryAttempts: 2, retryDelayMs: 1 }), { message: failure })
  assert.equal(calls, 1, 'model failure must not trigger an automatic retry')

  calls = 0
  const controller = new AbortController()
  globalThis.fetch = async () => { calls++; throw new TypeError('network interrupted') }
  const stopped = streamChat(payload, () => {}, { signal: controller.signal, retryDelayMs: 1000 })
  setTimeout(() => controller.abort(), 10)
  await assert.rejects(stopped, { name: 'AbortError' })
  assert.equal(calls, 1, 'stopping during backoff must not send a second request')

  calls = 0
  globalThis.fetch = async (_url, options) => {
    calls++
    return new Response(new ReadableStream({ start(stream) {
      options?.signal?.addEventListener('abort', () => stream.error(new DOMException('Aborted', 'AbortError')), { once: true })
    } }))
  }
  const active = new AbortController()
  const reading = streamChat(payload, () => {}, { signal: active.signal })
  setTimeout(() => active.abort(), 10)
  await assert.rejects(reading, { name: 'AbortError' })
  assert.equal(calls, 1)
  const recovered: string[] = []
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/chat/requests/saved-request/stream')
    assert.equal(options?.method, 'GET')
    assert.equal(options?.body, undefined, 'recovery must not submit another question or require the image again')
    return new Response('event: meta\ndata: {"conversation_id":42}\n\nevent: chunk\ndata: {"content":"先圈出已知条件。"}\n\nevent: done\ndata: {"content":"先圈出已知条件。"}\n\n')
  }
  await streamChat(payload, ({ event }) => recovered.push(event), { resumeRequestId: 'saved-request' })
  assert.deepEqual(recovered, ['meta', 'chunk', 'done'])
  console.log('stream failure / retry cancellation / active cancellation: passed')
} finally {
  await rm(dir, { recursive: true, force: true })
}
