import assert from 'node:assert/strict'

import { renderRichText } from '../src/utils/richText.ts'

const compactTable = '| 直接引语 | 间接引语 | |---------|---------| | I / my | 主语→he/she；宾语→his/her | | you / your | 根据意思选择 | | he/she/they | 不变 |'
const renderedCompact = renderRichText(compactTable)

assert.match(renderedCompact, /<table>/, 'compact markdown table should render as an HTML table')
assert.match(renderedCompact, /<th>直接引语<\/th>/, 'table header should render first column')
assert.match(renderedCompact, /<td>主语→he\/she；宾语→his\/her<\/td>/, 'table cells should stay aligned')

const multilineTable = [
  '| 直接引语 | 间接引语 |',
  '|---------|---------|',
  '| I / my | 主语→he/she；宾语→his/her |',
  '| you / your | 根据意思选择 |',
].join('\n')
const renderedMultiline = renderRichText(multilineTable)

assert.match(renderedMultiline, /<table>/, 'standard markdown table should render as an HTML table')
