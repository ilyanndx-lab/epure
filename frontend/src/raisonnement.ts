/**
 * Ce que le toggle « Réflexion du modèle » peut vraiment faire, selon le
 * fournisseur du modèle actif.
 *
 * Extrait de `ModuleBar.tsx` (qui portait ce calcul localement) pour que
 * `modules/chat/Component.tsx` puisse rendre le même toggle dans son popover
 * fusionné sans dupliquer la règle. `raisonnement` n'a pas le même effet
 * partout (`core/llm.py::stream`) : `gemini` l'ignore intégralement (aucune
 * bascule n'existe côté SDK), les cinq autres fournisseurs OpenAI-compatibles
 * cloud (groq, cerebras, mistral, nvidia, deepseek) — et depuis LM Studio,
 * `lmstudio` — ne l'utilisent que pour relever le plafond de tokens
 * (`_budget`), sans jamais faire remonter de réflexion visible — seuls
 * `ollama` et `flm` en affichent une. `lmstudio` n'est PAS un cloud (serveur
 * local, cf. `core/instance.py:_FOURNISSEURS_CLOUD`), mais rejoint quand même
 * ce groupe côté bascule : LM Studio n'expose aucun paramètre API stable pour
 * couper sa réflexion.
 */
export function capacitesRaisonnement(provider: string | undefined) {
  return {
    nonSupporte: provider === 'gemini',
    budgetSeul: !!provider && provider !== 'gemini' && provider !== 'ollama' && provider !== 'flm',
  }
}
