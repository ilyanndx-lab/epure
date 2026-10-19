/**
 * Ce que le réglage « Réflexion du modèle » peut vraiment faire, selon le
 * fournisseur du modèle actif.
 *
 * Extrait de `ModuleBar.tsx` (qui portait ce calcul localement) pour que
 * `modules/chat/Component.tsx` puisse rendre le même réglage dans son popover
 * fusionné sans dupliquer la règle. `raisonnement` n'a pas le même effet
 * partout (`core/llm.py::stream`) : `gemini` l'ignore intégralement (aucune
 * bascule n'existe côté SDK), les cinq fournisseurs OpenAI-compatibles cloud
 * (groq, cerebras, mistral, nvidia, deepseek) ne l'utilisent que pour relever
 * le plafond de tokens (`_budget`), sans jamais faire remonter de réflexion
 * visible. `ollama`, `flm` et — depuis la mesure du 2026-10-19 (#76) —
 * `lmstudio` affichent leur réflexion et savent la couper
 * (`reasoning_effort: "none"` pour LM Studio).
 */
export function capacitesRaisonnement(provider: string | undefined) {
  return {
    nonSupporte: provider === 'gemini',
    budgetSeul: !!provider && !['gemini', 'ollama', 'flm', 'lmstudio'].includes(provider),
  }
}

/**
 * Réglage de raisonnement : trois états (#76), stockés tels quels par le
 * backend (`core/memory.py::normaliser_raisonnement`).
 *
 * - `auto` (défaut) : le modèle raisonne, sauf sur un tour de lecture d'URL ou
 *   de recherche web avec un modèle local, où sa réflexion vidait le plafond de
 *   sortie (réponse vide, mesuré) ;
 * - `active` : raisonne partout, ce défaut compris ;
 * - `desactive` : ne raisonne nulle part.
 */
export type ModeRaisonnement = 'auto' | 'active' | 'desactive'

export const MODES_RAISONNEMENT: readonly ModeRaisonnement[] = ['auto', 'active', 'desactive']

/** Un des trois états, ou `auto` — y compris pour l'ancien booléen (migration),
 * même règle que le backend. */
export function normaliserModeRaisonnement(v: unknown): ModeRaisonnement {
  return typeof v === 'string' && (MODES_RAISONNEMENT as readonly string[]).includes(v)
    ? v as ModeRaisonnement
    : 'auto'
}
