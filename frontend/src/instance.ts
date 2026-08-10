import { useSyncExternalStore, useCallback } from 'react'

import { API, apiFetch } from './api'

/**
 * Store de configuration d'instance, calqué sur theme.ts (useSyncExternalStore).
 *
 * Source de vérité = le backend (GET /instance/config). localStorage sert de
 * cache optimiste : appliqué immédiatement au démarrage (évite le flash de
 * thème / de modules), puis remplacé par la config serveur dès qu'elle arrive.
 * Le thème est désormais géré ici (theme.ts ne fait plus que ré-exporter).
 */

const CACHE_KEY = 'epure-instance'

export type Theme = 'dark' | 'light'

export interface InstanceConfig {
  instance_id: string
  nom_affiché: string
  modules_activés: string[]
  providers: { actif: string; local: string; clés_présentes: Record<string, boolean> }
  fiches: { racine: string; watch_folders: string[] }
  atelier: {
    claude_path: string
    aider_path: string
    // api_key est absente des réponses du serveur : GET /instance/config
    // l'expurge (cf. core/instance.py::get) et ne renvoie que le booléen
    // dérivé api_key_présente. On ne peut l'envoyer que dans un PUT.
    gateway: {
      base_url: string
      model: string
      api_key?: string
      api_key_présente?: boolean
      start_command: string
    }
    moteur_defaut: string
    mode_defaut: string
  }
  thème: Theme
  preset_défaut: string | null
}

/** Patch partiel accepté par updateInstance (champs imbriqués partiels). */
export interface InstanceConfigPatch {
  nom_affiché?: string
  modules_activés?: string[]
  providers?: Partial<InstanceConfig['providers']>
  fiches?: Partial<InstanceConfig['fiches']>
  atelier?: {
    claude_path?: string
    aider_path?: string
    gateway?: Partial<InstanceConfig['atelier']['gateway']>
    moteur_defaut?: string
    mode_defaut?: string
  }
  thème?: Theme
  preset_défaut?: string | null
}

const DEFAULT_CONFIG: InstanceConfig = {
  instance_id: '',
  nom_affiché: 'Épure',
  // Vide, et non une liste en dur de modules du catalogue qui peuvent très bien
  // ne pas être installés. Côté backend, la liste vide signifie déjà « tous les
  // modules installés » (core/module_registry.active_ids) : c'est le seul défaut
  // qui ne puisse pas nommer un module absent.
  modules_activés: [],
  providers: { actif: 'qwen2.5:7b', local: 'qwen2.5:7b', clés_présentes: {} },
  fiches: { racine: '', watch_folders: [] },
  atelier: {
    claude_path: 'claude',
    aider_path: 'aider',
    gateway: { base_url: 'http://localhost:4000', model: '', api_key: '', start_command: '' },
    moteur_defaut: 'ollama',
    mode_defaut: 'headless',
  },
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
    const res = await apiFetch(`${API}/instance/config`)
    if (!res.ok) return
    const server = await res.json() as Partial<InstanceConfig>
    setConfig({ ...DEFAULT_CONFIG, ...server } as InstanceConfig)
  } catch {
    /* hors-ligne → on garde le cache optimiste */
  }
}

/** Merge partiel : applique en optimiste puis PUT serveur (qui fait foi). */
export async function updateInstance(partial: InstanceConfigPatch): Promise<void> {
  setConfig({
    ...config,
    ...partial,
    providers: { ...config.providers, ...(partial.providers ?? {}) },
    fiches: { ...config.fiches, ...(partial.fiches ?? {}) },
    atelier: {
      ...config.atelier,
      ...(partial.atelier ?? {}),
      gateway: { ...config.atelier.gateway, ...(partial.atelier?.gateway ?? {}) },
    },
  })
  try {
    const res = await apiFetch(`${API}/instance/config`, {
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
