import assert from 'node:assert/strict'

import { buildSubjectPieModel } from '../src/utils/subjectPieChart.ts'

const model = buildSubjectPieModel(
  [
    { subject: '数学', count: 30 },
    { subject: '物理', count: 15 },
    { subject: '化学', count: 5 },
  ],
  {
    colors: ['#111111', '#222222', '#333333'],
  },
)

assert.equal(model.total, 50)
assert.equal(model.slices.length, 3)
assert.deepEqual(
  model.slices.map((slice) => slice.label),
  ['数学 60.0%（30次）', '物理 30.0%（15次）', '化学 10.0%（5次）'],
)
assert.deepEqual(
  model.slices.map((slice) => slice.color),
  ['#111111', '#222222', '#333333'],
)
assert.ok(model.slices.every((slice) => slice.path.startsWith('M 150 150 L ')))
assert.ok(model.slices.every((slice) => slice.labelX < 150 || slice.labelX > 150))
assert.ok(model.slices.every((slice) => slice.labelY >= 24 && slice.labelY <= 276))

const emptyModel = buildSubjectPieModel([])
assert.equal(emptyModel.total, 0)
assert.deepEqual(emptyModel.slices, [])
