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

/** Insert at a DOM textarea selection (UTF-16 offsets), without changing other text. */
export function insertFormulaTemplate(text: string, start: number, end: number, template: FormulaTemplate) {
  const from = Math.max(0, Math.min(text.length, start))
  const to = Math.max(from, Math.min(text.length, end))
  const selected = text.slice(from, to)
  const wraps = template.before !== undefined && template.after !== undefined
  const inserted = wraps ? template.before! + (selected || 'x') + template.after! : template.source
  const selectionStart = wraps ? from + template.before!.length : from + inserted.length
  const selectionEnd = wraps ? selectionStart + (selected || 'x').length : selectionStart
  return {
    text: text.slice(0, from) + inserted + text.slice(to),
    selectionStart,
    selectionEnd,
  }
}
