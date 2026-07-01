import { useEffect, useState } from 'react'
import * as Icons from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import { API, apiFetch } from './api'

/**
 * Catalogue des modules (GET /modules), partagé entre Sidebar et Réglages.
 * Les icônes sont résolues dynamiquement par nom lucide-react.
 */

export interface ModuleManifest {
  id: string
  version: string
  nom: string
  icon: string
  description: string
  frontend: { component: string }
  backend: { prefix: string }
  core_module: boolean
  origin: string
  status: 'active' | 'disabled'
  removable: boolean
}

/** Résout une icône lucide-react par son nom (fallback : Box). */
export function resolveIcon(name: string): LucideIcon {
  const map = Icons as unknown as Record<string, LucideIcon>
  return map[name] ?? Icons.Box
}

let cache: ModuleManifest[] | null = null
const listeners = new Set<() => void>()

export async function fetchModules(): Promise<ModuleManifest[]> {
  try {
    const res = await apiFetch(`${API}/modules`)
    if (!res.ok) return cache ?? []
    const data = await res.json() as { modules: ModuleManifest[] }
    cache = data.modules
    listeners.forEach(l => l())
  } catch {
    /* hors-ligne : on garde le cache */
  }
  return cache ?? []
}

/** Catalogue réactif (chargé une fois, rafraîchissable). */
export function useModules(): ModuleManifest[] {
  const [mods, setMods] = useState<ModuleManifest[]>(cache ?? [])
  useEffect(() => {
    const sync = () => setMods(cache ?? [])
    listeners.add(sync)
    if (cache === null) void fetchModules()
    else sync()
    return () => { listeners.delete(sync) }
  }, [])
  return mods
}
