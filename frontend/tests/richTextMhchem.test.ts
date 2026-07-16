import assert from 'node:assert/strict'

import { renderRichText } from '../src/utils/richText.ts'

const rendered = renderRichText(String.raw`反应式：$\ce{2H2 + O2 -> 2H2O}$`)

assert.match(rendered, /class="katex"/, 'mhchem formula should render through KaTeX')
assert.doesNotMatch(rendered, /katex-error/, 'mhchem formula should not fall back to a KaTeX error')
