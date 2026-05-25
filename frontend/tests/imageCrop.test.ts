import assert from 'node:assert/strict'

import { clampCropRectToImage, previewRectToNaturalRect } from '../src/utils/imageCrop.ts'

const naturalRect = previewRectToNaturalRect(
  { x: 50, y: 20, width: 200, height: 100 },
  { width: 400, height: 200 },
  { width: 800, height: 600 },
)

assert.deepEqual(naturalRect, { x: 100, y: 60, width: 400, height: 300 })

assert.deepEqual(
  clampCropRectToImage({ x: -10, y: 20, width: 900, height: 700 }, { width: 800, height: 600 }),
  { x: 0, y: 20, width: 800, height: 580 },
)

assert.deepEqual(
  clampCropRectToImage({ x: 800, y: 600, width: 20, height: 20 }, { width: 800, height: 600 }),
  { x: 799, y: 599, width: 1, height: 1 },
)
