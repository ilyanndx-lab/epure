/**
 * Normalisation des corps de réponse, à poser à CHAQUE frontière `.json()`.
 *
 * Ces fonctions vivaient dans `components/ModuleBar.tsx`. Elles en sortent parce
 * que le chantier « conversations persistées » ajoute des frontières `.json()`
 * ailleurs (liste des conversations, chargement d'un fil, attachement de
 * fichiers) et que recopier la règle est le meilleur moyen de n'en corriger
 * qu'une des copies le jour venu.
 *
 * ── Pourquoi elles existent ───────────────────────────────────────────────────
 *
 * `r.json() as { files: string[] }` est une **affirmation**, pas une
 * vérification : TypeScript la croit sur parole et ne peut rien dire de la
 * réalité. Or plusieurs réponses parfaitement normales d'une application locale
 * n'ont pas cette forme — le 500 du gestionnaire d'exceptions
 * (`{"detail": …, "type": …}`), le 401 tant que le token n'est pas appairé, le
 * 404 sur une instance qui n'a pas la route, le 503 pendant la préparation de la
 * pile d'embedding. Le champ annoncé est alors `undefined`, l'état devient
 * `undefined`, et le `.catch()` ne voit rien puisque `r.json()` a parfaitement
 * réussi. La faute n'apparaît qu'au rendu suivant, sur un `.length` — et dans un
 * bundle minifié la trace ne nomme même pas la ligne.
 *
 * L'incident d'origine : `GET /rag/files` répondait 500 dans un paquet livré,
 * `availableFiles` passait à `undefined`, et l'ouverture du panneau fichiers du
 * module Docs levait « Cannot read properties of undefined (reading 'length') ».
 * Vérifié par `ModuleBar.test.tsx`, et par `ConversationList.test.tsx` pour les
 * frontières ajoutées depuis.
 */

/**
 * Tableau garanti.
 *
 * `Array.isArray` et non `?? []` : le second laisse passer `null` transformé en
 * tableau, mais aussi une chaîne ou un objet, qui replantent plus loin.
 */
export function liste<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

/** Dictionnaire sûr : `'x' in "une chaîne"` lève, un tableau ne dit rien. */
export function dico(v: unknown): Record<string, boolean> {
  return v && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, boolean>)
    : {}
}

export interface ModeleDisponible {
  id: string
  nom: string
  disponible: boolean
}

/**
 * Aplati la réponse de `GET /models` (`local`, `local_npu`,
 * `cloud.{rapide,puissant,long_contexte}`) en une liste unique, dédupliquée
 * par id — un même modèle peut apparaître dans plusieurs catégories (ex.
 * recommandé pour un rôle ET présent dans la liste complète).
 *
 * `ModuleBar.tsx` garde sa propre variante de cet aplatissement (dette non
 * traitée ici) ; `settings/Component.tsx` et le module chat (sélection des
 * modèles à comparer) passent tous les deux par celle-ci plutôt que d'en
 * réécrire une copie chacun.
 */
export function modelesDisponibles(v: unknown): ModeleDisponible[] {
  const o = (v ?? {}) as Record<string, unknown>
  const cloud = (o.cloud ?? {}) as Record<string, unknown>
  const brut = [
    ...liste<Record<string, unknown>>(o.local),
    ...liste<Record<string, unknown>>(o.local_npu),
    ...liste<Record<string, unknown>>(cloud.rapide),
    ...liste<Record<string, unknown>>(cloud.puissant),
    ...liste<Record<string, unknown>>(cloud.long_contexte),
  ]
  const parId = new Map<string, ModeleDisponible>()
  for (const m of brut) {
    const id = texte(m.id)
    if (!id || parId.has(id)) continue
    parId.set(id, { id, nom: texte(m.nom) || id, disponible: Boolean(m.disponible) })
  }
  return [...parId.values()]
}

/**
 * Chaîne garantie — utile là où le backend rend un titre ou un identifiant.
 *
 * Un `titre` absent devient `''` et non `"undefined"`, qui est ce qu'affiche un
 * `${d.titre}` sur un corps d'erreur.
 */
export function texte(v: unknown): string {
  return typeof v === 'string' ? v : ''
}
