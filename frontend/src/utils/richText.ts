import katex from 'katex'
import type { KnowledgeAsset } from './api'

type Segment =
  | { type: 'text'; content: string }
  | { type: 'math'; content: string; displayMode: boolean }
  | { type: 'code'; content: string; language: string }

type MathSegment = Extract<Segment, { type: 'text' | 'math' }>

const MATH_PLACEHOLDER_PREFIX = '\uE000'
const MATH_PLACEHOLDER_SUFFIX = '\uE001'
const INLINE_PLACEHOLDER_PREFIX = '\uE010'
const INLINE_PLACEHOLDER_SUFFIX = '\uE011'
const MATH_PLACEHOLDER_PATTERN = /\uE000(\d+)\uE001/g
const INLINE_PLACEHOLDER_PATTERN = /\uE010(\d+)\uE011/g
const INLINE_ASSET_MARKER_PATTERN = /【附图(\d+)(?:：[^】]+)?】/g

export interface InlineRichTextAsset {
  asset: KnowledgeAsset
  src: string
}

export interface RenderRichTextOptions {
  inlineAssets?: InlineRichTextAsset[]
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function isEscaped(text: string, index: number): boolean {
  let slashCount = 0
  for (let cursor = index - 1; cursor >= 0 && text[cursor] === '\\'; cursor -= 1) {
    slashCount += 1
  }
  return slashCount % 2 === 1
}

function findClosingDelimiter(text: string, delimiter: string, startIndex: number): number {
  let cursor = startIndex
  while (cursor < text.length) {
    const found = text.indexOf(delimiter, cursor)
    if (found === -1) {
      return -1
    }
    if (!isEscaped(text, found)) {
      return found
    }
    cursor = found + delimiter.length
  }
  return -1
}

function splitCodeSegments(text: string): Segment[] {
  const segments: Segment[] = []
  const pattern = /```([\w-]*)\n?([\s\S]*?)```/g
  let lastIndex = 0
  let match: RegExpExecArray | null

  match = pattern.exec(text)
  while (match) {
    if (match.index > lastIndex) {
      segments.push({ type: 'text', content: text.slice(lastIndex, match.index) })
    }
    segments.push({
      type: 'code',
      language: (match[1] || '').trim(),
      content: match[2].replace(/\n$/, ''),
    })
    lastIndex = match.index + match[0].length
    match = pattern.exec(text)
  }

  if (lastIndex < text.length) {
    segments.push({ type: 'text', content: text.slice(lastIndex) })
  }
  if (!segments.length) {
    return [{ type: 'text', content: text }]
  }
  return segments
}

function splitMathSegments(text: string): MathSegment[] {
  const segments: MathSegment[] = []
  let cursor = 0
  let plainStart = 0

  const pushPlain = (end: number) => {
    if (end > plainStart) {
      segments.push({ type: 'text', content: text.slice(plainStart, end) })
    }
  }

  while (cursor < text.length) {
    let token:
      | { open: string; close: string; displayMode: boolean }
      | null = null

    if (text.startsWith('$$', cursor) && !isEscaped(text, cursor)) {
      token = { open: '$$', close: '$$', displayMode: true }
    } else if (text.startsWith('\\[', cursor) && !isEscaped(text, cursor)) {
      token = { open: '\\[', close: '\\]', displayMode: true }
    } else if (text.startsWith('\\(', cursor) && !isEscaped(text, cursor)) {
      token = { open: '\\(', close: '\\)', displayMode: false }
    } else if (text[cursor] === '$' && !text.startsWith('$$', cursor) && !isEscaped(text, cursor)) {
      token = { open: '$', close: '$', displayMode: false }
    }

    if (!token) {
      cursor += 1
      continue
    }

    const contentStart = cursor + token.open.length
    const contentEnd = findClosingDelimiter(text, token.close, contentStart)
    if (contentEnd === -1) {
      cursor += token.open.length
      continue
    }

    const formula = text.slice(contentStart, contentEnd).trim()
    if (!formula) {
      cursor = contentEnd + token.close.length
      continue
    }

    pushPlain(cursor)
    segments.push({ type: 'math', content: formula, displayMode: token.displayMode })
    cursor = contentEnd + token.close.length
    plainStart = cursor
  }

  pushPlain(text.length)
  if (!segments.length) {
    return [{ type: 'text', content: text }]
  }
  return segments
}

function renderTextSegment(text: string): string {
  return escapeHtml(text).replace(/\n/g, '<br>')
}

function renderMathFallback(content: string, displayMode: boolean): string {
  const tag = displayMode ? 'div' : 'span'
  return `<${tag} class="message-math-fallback">${escapeHtml(content)}</${tag}>`
}

function renderMathSegment(content: string, displayMode: boolean): string {
  try {
    const html = katex.renderToString(content, {
      displayMode,
      throwOnError: false,
      strict: 'ignore',
      output: 'html',
    })
    return html.includes('katex-error') ? renderMathFallback(content, displayMode) : html
  } catch {
    return renderMathFallback(content, displayMode)
  }
}

function renderCodeSegment(content: string, language: string): string {
  const languageLabel = language ? `<div class="message-code__label">${escapeHtml(language)}</div>` : ''
  return `<pre class="message-code">${languageLabel}<code>${escapeHtml(content)}</code></pre>`
}

function createMathPlaceholder(index: number): string {
  return `${MATH_PLACEHOLDER_PREFIX}${index}${MATH_PLACEHOLDER_SUFFIX}`
}

function createInlinePlaceholder(index: number): string {
  return `${INLINE_PLACEHOLDER_PREFIX}${index}${INLINE_PLACEHOLDER_SUFFIX}`
}

function renderTextWithMathPlaceholders(text: string): { content: string; mathHtml: string[] } {
  const mathHtml: string[] = []
  const content = splitMathSegments(text)
    .map((segment) => {
      if (segment.type === 'text') {
        return segment.content
      }
      const index = mathHtml.push(renderMathSegment(segment.content, segment.displayMode)) - 1
      return createMathPlaceholder(index)
    })
    .join('')
  return { content, mathHtml }
}

function restoreMathPlaceholders(html: string, mathHtml: string[]): string {
  return html.replace(MATH_PLACEHOLDER_PATTERN, (_, index: string) => mathHtml[Number(index)] || '')
}

function restoreInlinePlaceholders(html: string, inlineHtml: string[]): string {
  return html.replace(INLINE_PLACEHOLDER_PATTERN, (_, index: string) => inlineHtml[Number(index)] || '')
}

function sanitizeHref(rawHref: string): string | null {
  const href = rawHref.trim()
  if (!href) {
    return null
  }
  if (/^(https?:|mailto:)/i.test(href)) {
    return href
  }
  return null
}

function renderInlineAsset(assetSource: InlineRichTextAsset): string {
  const label = assetSource.asset.title || assetSource.asset.filename || '题图'
  if (!assetSource.src) {
    return `<span class="message-inline-asset message-inline-asset--pending">${escapeHtml(label)}</span>`
  }
  const safeLabel = escapeHtml(label)
  const safeSrc = escapeHtml(assetSource.src)
  return `<a class="message-inline-asset" href="${safeSrc}" target="_blank" rel="noreferrer" title="${safeLabel}"><img src="${safeSrc}" alt="${safeLabel}" loading="lazy"></a>`
}

export function collectInlineAssetIds(text: string, assets: KnowledgeAsset[]): Set<string> {
  const assetIds = new Set<string>()
  for (const match of text.matchAll(INLINE_ASSET_MARKER_PATTERN)) {
    const asset = assets[Number(match[1]) - 1]
    if (asset?.content_type.startsWith('image/')) {
      assetIds.add(asset.asset_id)
    }
  }
  return assetIds
}

function protectInlineTokens(
  text: string,
  options: RenderRichTextOptions,
): { content: string; inlineHtml: string[] } {
  const inlineHtml: string[] = []
  let content = text.replace(/`([^`\n]+)`/g, (_, code: string) => {
    const index = inlineHtml.push(`<code>${escapeHtml(code)}</code>`) - 1
    return createInlinePlaceholder(index)
  })

  content = content.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match: string, label: string, href: string) => {
    const safeHref = sanitizeHref(href)
    const rendered = safeHref
      ? `<a href="${escapeHtml(safeHref)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`
      : escapeHtml(match)
    const index = inlineHtml.push(rendered) - 1
    return createInlinePlaceholder(index)
  })

  content = content.replace(INLINE_ASSET_MARKER_PATTERN, (match: string, order: string) => {
    const assetSource = options.inlineAssets?.[Number(order) - 1]
    if (!assetSource || !assetSource.asset.content_type.startsWith('image/')) {
      return match
    }
    const index = inlineHtml.push(renderInlineAsset(assetSource)) - 1
    return createInlinePlaceholder(index)
  })

  return { content, inlineHtml }
}

function renderInlineMarkdown(text: string, mathHtml: string[], options: RenderRichTextOptions): string {
  const { content, inlineHtml } = protectInlineTokens(text, options)
  const html = escapeHtml(content)
    .replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^\n]+?)__/g, '<strong>$1</strong>')
    .replace(/~~([^\n]+?)~~/g, '<del>$1</del>')

  return restoreMathPlaceholders(restoreInlinePlaceholders(html, inlineHtml), mathHtml)
}

function renderParagraph(lines: string[], mathHtml: string[], options: RenderRichTextOptions): string {
  return `<p>${renderInlineMarkdown(lines.join('\n'), mathHtml, options).replace(/\n/g, '<br>')}</p>`
}

function renderListItem(lines: string[], mathHtml: string[], options: RenderRichTextOptions): string {
  return `<li>${renderInlineMarkdown(lines.join('\n'), mathHtml, options).replace(/\n/g, '<br>')}</li>`
}

function matchHeading(line: string): RegExpMatchArray | null {
  return line.match(/^\s{0,3}(#{1,6})\s+(.*)$/)
}

function matchUnorderedList(line: string): RegExpMatchArray | null {
  return line.match(/^\s{0,3}[-+*]\s+(.*)$/)
}

function matchOrderedList(line: string): RegExpMatchArray | null {
  return line.match(/^\s{0,3}(\d+)[.)]\s+(.*)$/)
}

function isHorizontalRule(line: string): boolean {
  return /^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})\s*$/.test(line)
}

function isBlockquote(line: string): boolean {
  return /^\s{0,3}>\s?/.test(line)
}

function isSpecialBlockStart(line: string): boolean {
  return Boolean(
    matchHeading(line)
    || matchUnorderedList(line)
    || matchOrderedList(line)
    || isHorizontalRule(line)
    || isBlockquote(line),
  )
}

function renderMarkdownBlocks(text: string, mathHtml: string[], options: RenderRichTextOptions): string {
  const lines = text.split('\n')
  const blocks: string[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]

    if (!line.trim()) {
      index += 1
      continue
    }

    if (isHorizontalRule(line)) {
      blocks.push('<hr>')
      index += 1
      continue
    }

    const heading = matchHeading(line)
    if (heading) {
      const level = heading[1].length
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2].trim(), mathHtml, options)}</h${level}>`)
      index += 1
      continue
    }

    if (isBlockquote(line)) {
      const quoteLines: string[] = []
      while (index < lines.length) {
        const candidate = lines[index]
        if (!candidate.trim()) {
          quoteLines.push('')
          index += 1
          continue
        }
        const quoteMatch = candidate.match(/^\s{0,3}>\s?(.*)$/)
        if (!quoteMatch) {
          break
        }
        quoteLines.push(quoteMatch[1])
        index += 1
      }
      blocks.push(`<blockquote>${renderMarkdownBlocks(quoteLines.join('\n'), mathHtml, options)}</blockquote>`)
      continue
    }

    const unordered = matchUnorderedList(line)
    const ordered = matchOrderedList(line)
    if (unordered || ordered) {
      const orderedStart = ordered ? Number(ordered[1]) : null
      const tag = ordered ? 'ol' : 'ul'
      const items: string[] = []
      let currentItemLines = [ordered ? ordered[2] : unordered![1]]
      index += 1

      while (index < lines.length) {
        const candidate = lines[index]
        if (!candidate.trim()) {
          break
        }
        const nextUnordered = matchUnorderedList(candidate)
        const nextOrdered = matchOrderedList(candidate)
        if ((tag === 'ul' && nextUnordered) || (tag === 'ol' && nextOrdered)) {
          items.push(renderListItem(currentItemLines, mathHtml, options))
          currentItemLines = [tag === 'ol' ? nextOrdered![2] : nextUnordered![1]]
          index += 1
          continue
        }
        if (matchHeading(candidate) || isHorizontalRule(candidate) || isBlockquote(candidate)) {
          break
        }
        currentItemLines.push(candidate.trim())
        index += 1
      }

      items.push(renderListItem(currentItemLines, mathHtml, options))
      const startAttr = tag === 'ol' && orderedStart && orderedStart !== 1 ? ` start="${orderedStart}"` : ''
      blocks.push(`<${tag}${startAttr}>${items.join('')}</${tag}>`)
      continue
    }

    const paragraphLines = [line]
    index += 1
    while (index < lines.length) {
      const candidate = lines[index]
      if (!candidate.trim() || isSpecialBlockStart(candidate)) {
        break
      }
      paragraphLines.push(candidate)
      index += 1
    }
    blocks.push(renderParagraph(paragraphLines, mathHtml, options))
  }

  return blocks.join('')
}

export function renderRichText(content: string, options: RenderRichTextOptions = {}): string {
  const normalized = content.replace(/\r\n?/g, '\n')
  return splitCodeSegments(normalized)
    .map((segment) => {
      if (segment.type === 'code') {
        return renderCodeSegment(segment.content, segment.language)
      }
      const { content: textWithMathPlaceholders, mathHtml } = renderTextWithMathPlaceholders(segment.content)
      return renderMarkdownBlocks(textWithMathPlaceholders, mathHtml, options)
    })
    .join('')
}
