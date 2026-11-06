// Images stay in memory for route changes; only text survives a tab refresh.
const PREFIX = 'studyagent-feedback-draft:v1:'
const drafts = new Map<number, { text: string; files: File[] }>()

export function loadFeedbackDraft(ownerId: number): { text: string; files: File[] } {
  const cached = drafts.get(ownerId)
  if (cached) return { text: cached.text, files: [...cached.files] }
  let text = ''
  try { text = window.sessionStorage.getItem(`${PREFIX}${ownerId}`) || '' } catch { /* Memory-only fallback. */ }
  return { text, files: [] }
}

export function saveFeedbackDraft(ownerId: number, text: string, files: File[]) {
  if (!text && !files.length) drafts.delete(ownerId)
  else drafts.set(ownerId, { text, files: [...files] })
  try {
    if (text) window.sessionStorage.setItem(`${PREFIX}${ownerId}`, text)
    else window.sessionStorage.removeItem(`${PREFIX}${ownerId}`)
  } catch { /* Keep editing when browser storage is unavailable or full. */ }
}

export function clearFeedbackDrafts() {
  drafts.clear()
  try {
    const storage = window.sessionStorage
    for (let i = storage.length - 1; i >= 0; i--) {
      const key = storage.key(i)
      if (key?.startsWith(PREFIX)) storage.removeItem(key)
    }
  } catch { /* In-memory drafts are already cleared. */ }
}
