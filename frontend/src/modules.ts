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

/**
 * Modules à afficher, dans l'ordre — miroir exact de `core.module_registry.active_ids`.
 *
 * ⚠️ `modules_activés` VIDE ne signifie pas « aucun module » mais « jamais
 * initialisée ». Le backend y répond « tous les modules installés, dans l'ordre
 * du catalogue », et monte leurs routeurs en conséquence
 * (`core/module_registry.py`, règle 1).
 *
 * Le frontend, lui, la lisait littéralement : il exigeait l'appartenance à la
 * liste. Sur une INSTALLATION NEUVE — le seul cas où elle est vide — la barre
 * ne montrait donc que Réglages, alors que `GET /modules` annonçait tous les
 * modules `active` et que le backend servait leurs routes. Invisible sur un
 * poste existant, dont la liste est peuplée depuis longtemps ; fatal pour
 * quiconque installe Épure pour la première fois.
 */
export function orderedModules(
  modules: ModuleManifest[],
  actifs: readonly string[],
): ModuleManifest[] {
  const actifsCatalogue = modules.filter(m => m.status === 'active')
  if (actifs.length === 0) return actifsCatalogue
  const byId = new Map(actifsCatalogue.map(m => [m.id, m]))
  return actifs
    .map(id => byId.get(id))
    .filter((m): m is ModuleManifest => !!m)
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
