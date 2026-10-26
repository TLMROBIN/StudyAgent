import assert from 'node:assert/strict'
import { installViewportHeight } from '../src/utils/viewportHeight.ts'

const properties: Record<string, string> = {}
const events = new Map<string, () => void>()
const visualEvents = new Map<string, () => void>()
const root = { style: { setProperty: (key: string, value: string) => { properties[key] = value } }, dataset: {} as Record<string, string> }
const win = {
  innerHeight: 768,
  visualViewport: { height: 768, offsetTop: 0,
    addEventListener: (name: string, fn: () => void) => { visualEvents.set(name, fn) },
    removeEventListener: (name: string) => { visualEvents.delete(name) },
  },
  document: { documentElement: root },
  addEventListener: (name: string, fn: () => void) => { events.set(name, fn) },
  removeEventListener: (name: string) => { events.delete(name) },
}
const stop = installViewportHeight(win)
assert.equal(properties['--app-viewport-height'], '768px')
win.visualViewport.height = 320
win.visualViewport.offsetTop = 100
visualEvents.get('resize')!()
assert.equal(properties['--app-viewport-height'], '320px')
assert.equal(properties['--app-viewport-offset-top'], '100px')
assert.equal(root.dataset.compactViewport, 'true')
win.visualViewport.height = 768
win.visualViewport.offsetTop = 0
visualEvents.get('resize')!()
assert.equal(root.dataset.compactViewport, 'false')
stop()
assert.equal(events.size + visualEvents.size, 0)
