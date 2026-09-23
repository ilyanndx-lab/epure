/**
 * Bouton « Recherche approfondie » : budget RÉELLEMENT configuré et libellés
 * qui en dépendent — extraits de Component.tsx pour être testables sans
 * monter le composant, même raison que `traceRecherche.ts`.
 *
 * L'info-bulle disait « jusqu'à 4 requêtes » en dur, alors que le budget se
 * règle de 1 à 10 (Réglages › Tool calling, `core.memory._BUDGET_RECHERCHE_
 * APPROFONDIE_*`) : réglé à 8, le bouton en annonçait toujours 4. C'est ce
 * budget que le backend applique au tour (`budgets_override`,
 * `modules/chat/router.py`), que le skill soit activé dans les Réglages ou
 * seulement forcé par ce bouton.
 */

/**
 * Budget de `recherche_approfondie` lu dans une réponse `GET /context`, ou
 * `null` s'il est absent ou invalide — auquel cas le libellé ne cite AUCUN
 * nombre plutôt qu'un nombre inventé.
 */
export function budgetRechercheApprofondie(contexte: unknown): number | null {
  if (!contexte || typeof contexte !== 'object') return null
  const tc = (contexte as Record<string, unknown>)['tool_calling']
  const skills = tc && typeof tc === 'object' ? (tc as Record<string, unknown>)['skills'] : null
  const ra = skills && typeof skills === 'object'
    ? (skills as Record<string, unknown>)['recherche_approfondie']
    : null
  const budget = ra && typeof ra === 'object' ? (ra as Record<string, unknown>)['budget'] : null
  return typeof budget === 'number' && Number.isInteger(budget) && budget > 0 ? budget : null
}

/** Info-bulle du bouton, avec le budget configuré s'il est connu. */
export function infoBulleRechercheApprofondie(budget: number | null): string {
  const combien = budget === null
    ? 'plusieurs requêtes'
    : `jusqu'à ${budget} requête${budget > 1 ? 's' : ''}`
  return `Autorise le modèle à enchaîner plusieurs recherches web pour reformuler et creuser le sujet (${combien}), pour CE message uniquement`
}
