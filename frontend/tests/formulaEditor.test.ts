import assert from 'node:assert/strict'
import { formulaTemplates, formulaExamples, insertFormulaTemplate } from '../src/utils/formulaEditor.ts'
import { renderRichText } from '../src/utils/richText.ts'

const inline = formulaTemplates[0]
assert.deepEqual(insertFormulaTemplate('前后', 1, 1, inline), { text: '前$x$后', selectionStart: 2, selectionEnd: 3 })
assert.deepEqual(insertFormulaTemplate('前a+b后', 1, 4, inline), { text: '前$a+b$后', selectionStart: 2, selectionEnd: 5 })
const display = insertFormulaTemplate('😀中文', 2, 4, formulaTemplates[1])
assert.equal(display.text, '😀$$\n中文\n$$')
assert.equal(display.text.slice(display.selectionStart, display.selectionEnd), '中文')
const fraction = insertFormulaTemplate('abc', 1, 2, formulaTemplates[2])
assert.equal(fraction.text, String.raw`a$\frac{a}{b}$c`)
assert.equal(fraction.selectionStart, fraction.selectionEnd)
const continued = insertFormulaTemplate(fraction.text, fraction.selectionEnd, fraction.selectionEnd, inline)
assert.equal(continued.text, String.raw`a$\frac{a}{b}$$x$c`)
assert.equal(insertFormulaTemplate('', 100, 100, inline).text, '$x$')
// Replace the radical's placeholder with a fraction without nesting delimiters.
for (const [open, close] of [['$', '$'], ['$$\n', '\n$$'], ['\\(', '\\)'], ['\\[', '\\]']]) {
  const source = `${open}\\sqrt{x}${close}`
  const at = source.indexOf('x')
  const nested = insertFormulaTemplate(source, at, at + 1, formulaTemplates[2])
  assert.equal(nested.text, `${open}\\sqrt{\\frac{a}{b}}${close}`)
  assert.doesNotMatch(renderRichText(nested.text), /message-math-fallback|katex-error/)
  const unchanged = insertFormulaTemplate(source, at, at + 1, inline)
  assert.equal(unchanged.text, source)
  assert.equal(unchanged.text.slice(unchanged.selectionStart, unchanged.selectionEnd), 'x')
}
assert.equal(insertFormulaTemplate('$x$', 1, 1, formulaTemplates[4]).text, '$\\sqrt{x}x$')
assert.equal(insertFormulaTemplate('$\\sqrt{', 7, 7, formulaTemplates[2]).text, '$\\sqrt{\\frac{a}{b}')
assert.equal(insertFormulaTemplate('\\$5 后', 5, 5, formulaTemplates[2]).text, '\\$5 后$\\frac{a}{b}$')
for (const code of ['`$x$`', '```latex\n$x$\n```']) {
  const at = code.indexOf('x')
  assert.equal(insertFormulaTemplate(code, at, at + 1, formulaTemplates[2]).text,
    code.slice(0, at) + '$\\frac{a}{b}$' + code.slice(at + 1))
}
assert.equal(insertFormulaTemplate('$x$ 后 $y$', 5, 5, formulaTemplates[2]).text, '$x$ 后$\\frac{a}{b}$ $y$')
for (const example of formulaExamples) {
  const html = renderRichText(example.source)
  assert.doesNotMatch(html, /message-math-fallback|katex-error/, example.label)
  if (/\$|\\\(|\\\[/.test(example.source)) assert.match(html, /class="katex/, example.label)
}
assert.match(renderRichText('$\\frac{a}{b$'), /message-math-fallback/)
assert.ok(renderRichText('未完成 $\\frac{a}').includes('未完成'))
assert.doesNotMatch(renderRichText('<img src=x onerror=alert(1)><script>alert(1)</script>'), /<img|<script/)
assert.doesNotMatch(renderRichText('```text\n$x$\n```'), /class="katex/)
