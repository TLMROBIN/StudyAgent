export interface FormulaTemplate {
  label: string
  source: string
  before?: string
  after?: string
}

export const formulaTemplates: FormulaTemplate[] = [
  { label: '行内公式', source: '$x$', before: '$', after: '$' },
  { label: '独立公式', source: '$$\nx\n$$', before: '$$\n', after: '\n$$' },
  { label: '分式', source: String.raw`$\frac{a}{b}$` },
  { label: '上下标', source: '$x_{1}^{2}$' },
  { label: '根号', source: String.raw`$\sqrt{x}$` },
  { label: '向量', source: String.raw`$\vec{F}$` },
  { label: '希腊字母', source: String.raw`$\alpha + \beta$` },
  { label: '多行公式', source: String.raw`$$
\begin{aligned}
a &= b + c \\
x &= y + z
\end{aligned}
$$` },
]

export const formulaExamples: FormulaTemplate[] = [
  ...formulaTemplates,
  { label: '兼容行内写法', source: String.raw`\(x + y\)` },
  { label: '兼容独立写法', source: String.raw`\[x + y\]` },
  { label: '单位与间距', source: String.raw`$v = 5\,\mathrm{m/s}$` },
  { label: '化学式', source: String.raw`$\ce{2H2 + O2 -> 2H2O}$` },
  { label: '标题', source: '## 我的思路' },
  { label: '加粗', source: '**已知条件**' },
  { label: '列表', source: '- 第一个条件\n- 第二个条件' },
]

/** Find math content boundaries, skipping Markdown code and escaped delimiters. */
function mathContentAt(text: string, start: number, end: number): boolean {
  const tokens = /(`+)[\s\S]*?\1|\\\\|\\\$|\$\$|\$|\\[()[\]]/g
  let open: { close: string; start: number } | undefined
  for (const match of text.matchAll(tokens)) {
    const token = match[0]
    const index = match.index!
    if (open) {
      if (token === open.close) {
        if (start >= open.start && end <= index) return true
        open = undefined
      }
    } else if (token === '$' || token === '$$' || token === '\\(' || token === '\\[') {
      open = { close: token === '\\(' ? '\\)' : token === '\\[' ? '\\]' : token, start: index + token.length }
    }
  }
  // Also support an unfinished formula while the student is still typing.
  return !!open && start >= open.start
}

function formulaContent(source: string): string {
  for (const [before, after] of [['$$', '$$'], ['$', '$'], ['\\(', '\\)'], ['\\[', '\\]']]) {
    if (source.startsWith(before) && source.endsWith(after)) {
      return source.slice(before.length, -after.length)
    }
  }
  return source
}

/** Insert at a DOM textarea selection (UTF-16 offsets), without changing other text. */
export function insertFormulaTemplate(text: string, start: number, end: number, template: FormulaTemplate) {
  const from = Math.max(0, Math.min(text.length, start))
  const to = Math.max(from, Math.min(text.length, end))
  const selected = text.slice(from, to)
  const wraps = template.before !== undefined && template.after !== undefined
  const insideMath = mathContentAt(text, from, to)
  const content = selected || 'x'
  const inserted = wraps
    ? insideMath ? content : template.before! + content + template.after!
    : insideMath ? formulaContent(template.source) : template.source
  const selectionStart = wraps ? from + (insideMath ? 0 : template.before!.length) : from + inserted.length
  const selectionEnd = wraps ? selectionStart + content.length : selectionStart
  return {
    text: text.slice(0, from) + inserted + text.slice(to),
    selectionStart,
    selectionEnd,
  }
}
