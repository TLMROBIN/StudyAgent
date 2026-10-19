import assert from 'node:assert/strict'
import katex from 'katex'
import { renderRichText } from '../src/utils/richText.ts'

const formula = String.raw`\sqrt{\dfrac{3}{8} - \sqrt{5}}`
const expectedPaths = [...katex.renderToString(formula, { output: 'html' }).matchAll(/<path d="([^"]+)"/g)]
  .map((match) => match[1])
assert.ok(expectedPaths.length >= 2, 'fixture must contain nested radical SVG paths')

for (const [label, text] of [
  ['paragraph', `先观察 $${formula}$，再判断符号。`],
  ['unordered list', `- 观察 $${formula}$\n- 判断符号`],
  ['ordered list', `1. 观察 $${formula}$\n2. 判断符号`],
  ['parenthesized math', `观察 \\(${formula}\\)`],
  ['display math', `\\[${formula}\\]`],
  ['dollar display math', `$$${formula}$$`],
  ['heading', `## 观察 $${formula}$`],
  ['blockquote', `> 观察 $${formula}$`],
  ['table', `| 式子 | 说明 |\n| --- | --- |\n| $${formula}$ | 观察 |`],
]) {
  const html = renderRichText(text)
  assert.match(html, /class="katex/, `${label}: formula must render`)
  assert.doesNotMatch(html, /katex-error|message-math-fallback/, `${label}: no math error`)
  for (const path of expectedPaths) {
    assert.ok(html.includes(`<path d="${path}"`), `${label}: preserve KaTeX radical path exactly`)
  }
  // Each SSE update renders the accumulated message, including incomplete math.
  for (let end = 1; end <= text.length; end += 1) {
    const partial = renderRichText(text.slice(0, end))
    for (const svg of partial.matchAll(/<svg[\s\S]*?<\/svg>/g)) {
      assert.doesNotMatch(svg[0], /<br>/, `${label}: stream must not corrupt SVG`)
    }
  }
}

const markdown = renderRichText('## 思考\n\n**重点**\n\n- 第一项\n- 第二项\n\n`<script>`\n\n```text\n原样代码\n第二行\n```')
assert.match(markdown, /<h2>思考<\/h2>/)
assert.match(markdown, /<strong>重点<\/strong>/)
assert.match(markdown, /<ul><li>第一项<\/li><li>第二项<\/li><\/ul>/)
assert.match(markdown, /<code>&lt;script&gt;<\/code>/)
assert.match(markdown, /<code>原样代码\n第二行<\/code>/)
assert.doesNotMatch(renderRichText('<img src=x onerror=alert(1)>'), /<img/)
