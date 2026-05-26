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
assert.ok(model.slices.every((slice) => slice.path.startsWith('M 380 170 L ')))
assert.ok(model.slices.every((slice) => slice.labelX < 380 || slice.labelX > 380))
assert.ok(model.slices.every((slice) => slice.labelY >= 24 && slice.labelY <= 316))

const emptyModel = buildSubjectPieModel([])
assert.equal(emptyModel.total, 0)
assert.deepEqual(emptyModel.slices, [])

const crowdedModel = buildSubjectPieModel([
  { subject: '数学', count: 703 },
  { subject: '语文', count: 428 },
  { subject: '物理', count: 349 },
  { subject: '化学', count: 299 },
  { subject: '英语', count: 185 },
  { subject: '生物', count: 136 },
  { subject: '历史', count: 94 },
  { subject: '政治', count: 70 },
  { subject: '地理', count: 46 },
])

for (const side of ['start', 'end'] as const) {
  const labelRows = crowdedModel.slices
    .filter((slice) => slice.textAnchor === side)
    .map((slice) => slice.labelY)
    .sort((first, second) => first - second)
  for (let index = 1; index < labelRows.length; index += 1) {
    assert.ok(labelRows[index] - labelRows[index - 1] >= 24, `${side} labels overlap at rows ${labelRows[index - 1]} and ${labelRows[index]}`)
  }
}
