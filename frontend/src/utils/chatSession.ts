interface SessionStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

const key = (student: string | number) => `studyagent:last-conversation:v1:${student}`

export function rememberConversation(storage: SessionStorage, student: string | number, id: number | null) {
  try { storage.setItem(key(student), JSON.stringify(id)) } catch { /* Restricted WebView. */ }
}

export function recalledConversation(storage: SessionStorage, student: string | number): number | null {
  try {
    const id = JSON.parse(storage.getItem(key(student)) || 'null')
    return Number.isSafeInteger(id) && id > 0 ? id : null
  } catch { return null }
}
