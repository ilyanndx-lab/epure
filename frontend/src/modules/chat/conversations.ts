import { API, apiFetch } from '../../api'
import { liste, texte } from '../../normaliser'

/**
 * Accès HTTP aux conversations du chat — étape 6 de docs/conversations-persistees.md.
 *
 * Séparé de `ConversationList.tsx` et pas seulement pour la forme : un fichier
 * qui exporte à la fois un composant et des fonctions casse le Fast Refresh de
 * Vite (`react-refresh/only-export-components`, une ERREUR eslint ici, pas un
 * avertissement). Le module voisin `commands.ts` suit la même règle.
 *
 * ── Toutes les réponses sont normalisées ──────────────────────────────────────
 *
 * `as {conversations: ConvEntry[]}` serait une affirmation, pas une
 * vérification. `GET /chat/conversations` répond 401 tant que le token n'est pas
 * appairé — fréquent au premier rendu — et le corps d'un 401 n'a pas de champ
 * `conversations` : l'état passerait à `undefined` et le `.map()` du rendu
 * planterait au tour suivant, dans un chunk minifié où la trace ne nomme même
 * pas la ligne. C'est l'incident du panneau fichiers du module Docs, rejoué.
 */

export interface ConvEntry {
  id: string
  titre: string
  date: string
  apercu: string
  n_messages: number
  modifiée: string
  n_fichiers: number
}

/** Une entrée d'index, quel que soit ce que le backend a réellement renvoyé. */
export function entree(v: unknown): ConvEntry {
  const o = (v ?? {}) as Record<string, unknown>
  return {
    id: texte(o.id),
    titre: texte(o.titre),
    date: texte(o.date),
    apercu: texte(o.apercu),
    n_messages: typeof o.n_messages === 'number' ? o.n_messages : 0,
    modifiée: texte(o['modifiée']),
    n_fichiers: typeof o.n_fichiers === 'number' ? o.n_fichiers : 0,
  }
}

/** Liste des conversations. Jamais d'exception : `[]` en cas de souci. */
export async function chargerConversations(): Promise<ConvEntry[]> {
  try {
    const res = await apiFetch(`${API}/chat/conversations`)
    if (!res.ok) return []
    const d = await res.json() as Record<string, unknown>
    return liste<unknown>(d.conversations).map(entree).filter(c => c.id)
  } catch {
    return []  // backend qui démarre, token pas encore appairé : sans gravité
  }
}

/**
 * Crée une conversation et rend son identifiant.
 *
 * Lève en cas d'échec, contrairement à `chargerConversations` : l'appelant vient
 * de cliquer « Nouvelle conversation », il doit pouvoir distinguer un échec d'un
 * résultat vide.
 */
export async function creerConversation(): Promise<string> {
  const res = await apiFetch(`${API}/chat/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const d = await res.json() as Record<string, unknown>
  return texte(d.id)
}

export async function renommerConversation(id: string, titre: string): Promise<boolean> {
  try {
    const res = await apiFetch(`${API}/chat/conversations/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ titre }),
    })
    return res.ok
  } catch {
    return false
  }
}

export async function supprimerConversation(id: string): Promise<boolean> {
  try {
    const res = await apiFetch(`${API}/chat/conversations/${id}`, { method: 'DELETE' })
    return res.ok
  } catch {
    return false
  }
}
