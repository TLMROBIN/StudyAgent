export const VIEWPORT_HEIGHT_PROPERTY = '--app-viewport-height'

type ViewportEventTarget = {
  addEventListener?: (type: string, listener: () => void) => void
  removeEventListener?: (type: string, listener: () => void) => void
}

type ViewportWindow = ViewportEventTarget & {
  innerHeight?: number
  visualViewport?: ViewportEventTarget & { height?: number | null; offsetTop?: number } | null
  document?: {
    documentElement?: {
      dataset?: Record<string, string>
      style?: {
        setProperty: (property: string, value: string) => void
      }
    }
  }
}

function defaultWindow(): ViewportWindow | null {
  if (typeof window === 'undefined') {
    return null
  }
  return window
}

function positiveHeight(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 0
}

export function measureViewportHeight(win: ViewportWindow | null = defaultWindow()): number {
  if (!win) {
    return 0
  }

  const innerHeight = positiveHeight(win.innerHeight)
  const visualHeight = positiveHeight(win.visualViewport?.height)

  if (innerHeight && visualHeight) {
    return Math.min(innerHeight, visualHeight)
  }
  return visualHeight || innerHeight
}

export function syncViewportHeight(win: ViewportWindow | null = defaultWindow()): number {
  const height = measureViewportHeight(win)
  const root = win?.document?.documentElement
  if (!height || !root?.style) {
    return height
  }

  root.style.setProperty(VIEWPORT_HEIGHT_PROPERTY, `${height}px`)
  root.style.setProperty('--app-viewport-offset-top', `${Math.max(0, win?.visualViewport?.offsetTop || 0)}px`)
  if (root.dataset) root.dataset.compactViewport = String(height < 500)
  return height
}

export function installViewportHeight(win: ViewportWindow | null = defaultWindow()): () => void {
  if (!win) {
    return () => {}
  }

  const sync = () => {
    syncViewportHeight(win)
    // Ordinary pages scroll; the fixed chat shell instead follows visualViewport.
    if (typeof document !== 'undefined') {
      const active = document.activeElement as HTMLElement | null
      if (active?.matches('.student-feedback-page textarea')) {
        active.scrollIntoView({ block: 'nearest' })
      }
    }
  }

  let focusTimer: ReturnType<typeof setTimeout> | undefined
  const focusSync = () => {
    clearTimeout(focusTimer)
    focusTimer = setTimeout(sync, 350)
  }
  sync()
  win.addEventListener?.('focusin', focusSync)
  win.addEventListener?.('resize', sync)
  win.addEventListener?.('orientationchange', sync)
  win.visualViewport?.addEventListener?.('resize', sync)
  win.visualViewport?.addEventListener?.('scroll', sync)

  return () => {
    clearTimeout(focusTimer)
    win.removeEventListener?.('focusin', focusSync)
    win.removeEventListener?.('resize', sync)
    win.removeEventListener?.('orientationchange', sync)
    win.visualViewport?.removeEventListener?.('resize', sync)
    win.visualViewport?.removeEventListener?.('scroll', sync)
  }
}
