import { useSyncExternalStore, useCallback } from 'react'

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'epure-theme'
const listeners = new Set<() => void>()

function readTheme(): Theme {
  return localStorage.getItem(STORAGE_KEY) === 'light' ? 'light' : 'dark'
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
}

/** À appeler une fois avant le premier render (évite le flash de thème). */
export function initTheme() {
  applyTheme(readTheme())
}

export function setTheme(theme: Theme) {
  localStorage.setItem(STORAGE_KEY, theme)
  applyTheme(theme)
  listeners.forEach(l => l())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** Thème courant + toggle, synchronisé entre tous les composants. */
export function useTheme(): { theme: Theme; toggleTheme: () => void } {
  const theme = useSyncExternalStore(subscribe, readTheme)
  const toggleTheme = useCallback(() => {
    setTheme(readTheme() === 'dark' ? 'light' : 'dark')
  }, [])
  return { theme, toggleTheme }
}
