import { useSyncExternalStore, useCallback } from 'react'

/**
 * Store de configuration d'instance, calqué sur theme.ts (useSyncExternalStore).
 *
 * Source de vérité = le backend (GET /instance/config). localStorage sert de
 * cache optimiste : appliqué immédiatement au démarrage (évite le flash de
 * thème / de modules), puis remplacé par la config serveur dès qu'elle arrive.
 * Le thème est désormais géré ici (theme.ts ne fait plus que ré-exporter).
 */

const API = 'http://localhost:8000'
const CACHE_KEY = 'epure-instance'

export type Theme = 'dark' | 'light'

export interface InstanceConfig {
  instance_id: string
  nom_affiché: string
  modules_activés: string[]
  providers: { actif: string; local: string; clés_présentes: Record<string, boolean> }
  fiches: { racine: string; watch_folders: string[] }
  thème: Theme
  preset_défaut: string | null
}

const DEFAULT_CONFIG: InstanceConfig = {
  instance_id: '',
  nom_affiché: 'Épure',
  modules_activés: ['chat', 'kholle', 'flashcards', 'code', 'docs', 'admin', 'history'],
  providers: { actif: 'qwen2.5:7b', local: 'qwen2.5:7b', clés_présentes: {} },
  fiches: { racine: '', watch_folders: [] },
  thème: 'dark',
  preset_défaut: null,
}

const listeners = new Set<() => void>()

function readCache(): InstanceConfig | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    return raw ? { ...DEFAULT_CONFIG, ...JSON.parse(raw) } : null
  } catch {
    return null
  }
}

let config: InstanceConfig = readCache() ?? DEFAULT_CONFIG

function writeCache(c: InstanceConfig) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(c))
  } catch {
    /* quota plein — sans gravité */
  }
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
}

function setConfig(next: InstanceConfig) {
  config = next
  writeCache(config)
  applyTheme(config.thème)
  listeners.forEach(l => l())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

function getSnapshot(): InstanceConfig {
  return config
}

/** Applique le cache immédiatement (anti-flash) puis va chercher la config serveur. */
export function initInstance() {
  applyTheme(config.thème)
  void refreshInstance()
}

/** Recharge la config depuis le serveur (silencieux si hors-ligne). */
export async function refreshInstance(): Promise<void> {
  try {
    const res = await fetch(`${API}/instance/config`)
    if (!res.ok) return
    const server = await res.json() as Partial<InstanceConfig>
    setConfig({ ...DEFAULT_CONFIG, ...server } as InstanceConfig)
  } catch {
    /* hors-ligne → on garde le cache optimiste */
  }
}

/** Merge partiel : applique en optimiste puis PUT serveur (qui fait foi). */
export async function updateInstance(partial: Partial<InstanceConfig>): Promise<void> {
  setConfig({
    ...config,
    ...partial,
    providers: { ...config.providers, ...(partial.providers ?? {}) },
    fiches: { ...config.fiches, ...(partial.fiches ?? {}) },
  })
  try {
    const res = await fetch(`${API}/instance/config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(partial),
    })
    if (res.ok) {
      const server = await res.json() as Partial<InstanceConfig>
      setConfig({ ...DEFAULT_CONFIG, ...server } as InstanceConfig)
    }
  } catch {
    /* garde l'optimiste */
  }
}

/** Config d'instance réactive, synchronisée entre tous les composants. */
export function useInstanceConfig(): InstanceConfig {
  return useSyncExternalStore(subscribe, getSnapshot)
}

// ── Thème (rétro-compatibilité avec theme.ts) ────────────────────────────────

export function useTheme(): { theme: Theme; toggleTheme: () => void } {
  const cfg = useSyncExternalStore(subscribe, getSnapshot)
  const toggleTheme = useCallback(() => {
    void updateInstance({ thème: getSnapshot().thème === 'dark' ? 'light' : 'dark' })
  }, [])
  return { theme: cfg.thème, toggleTheme }
}

export function setTheme(theme: Theme) {
  void updateInstance({ thème: theme })
}
