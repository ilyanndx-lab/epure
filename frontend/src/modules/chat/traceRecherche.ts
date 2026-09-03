/**
 * Trace @web (déroulé de recherche + validation des citations) : normalisation,
 * résumé et libellés — extraits de Component.tsx pour être testables sans
 * monter le composant, même raison que `metaMessage.ts`.
 *
 * ── La distinction que ce module sert à préserver ─────────────────────────
 *
 * Une étape `citations_invalides` peut faire une affirmation de DEUX forces
 * différentes, portées par son champ `verifiees_contre` (core/citations.py
 * ne change pas ; c'est modules/chat/router.py qui l'étiquette) :
 *
 * | verifiees_contre | ce qui s'est passé | libellé |
 * |---|---|---|
 * | `"recherche"` | une recherche @web a rendu des résultats, l'URL/le rang cité n'en fait pas partie | « Citation hors sources » — affirmation FORTE |
 * | `"aucune_source"` | aucune recherche ce tour, aucune source RAG portant cette URL | « Lien non vérifié » — affirmation FAIBLE |
 *
 * Confondre les deux ferait crier au loup à chaque URL de mémoire non
 * vérifiée — un modèle a parfaitement le droit de citer correctement une URL
 * connue sans qu'aucune recherche n'ait eu lieu. Le badge finirait ignoré.
 *
 * ⚠️ **`trace_recherche` peut ne contenir QUE `citations_invalides`**, sans
 * aucune étape de recherche : @web est un override manuel rare, la majorité
 * des tours n'en déclenchent aucune, et c'est précisément là que la
 * validation des citations est la plus utile. Le résumé ne doit alors JAMAIS
 * annoncer une recherche qui n'a pas eu lieu.
 */

import { liste, texte } from '../../normaliser'

/**
 * Une étape de trace. Volontairement PAS un type fermé par `etape` (union
 * discriminée) : le schéma est extensible par construction (une future phase
 * ajoutera d'autres étapes sans migration ni contrat changé) — le rendu gère
 * les types connus et affiche un repli générique pour tout le reste plutôt
 * que de les faire disparaître.
 */
export interface EtapeTrace {
  etape: string
  [cle: string]: unknown
}

/** `trace_recherche` normalisé à chaque frontière `.json()`/WebSocket, comme
 * `liste`/`texte` (`../../normaliser`) : une étape sans son discriminant
 * `etape` n'est pas exploitable, elle est écartée plutôt que de planter le
 * rendu plus loin. */
export function etapesDe(v: unknown): EtapeTrace[] {
  return liste<Record<string, unknown>>(v)
    .map(e => ({ ...e, etape: texte(e.etape) }))
    .filter((e): e is EtapeTrace => e.etape !== '')
}

/**
 * Une étape `citations_invalides` fait-elle une affirmation FORTE (vérifiée
 * contre de vrais résultats de recherche) ou FAIBLE (aucune source à
 * comparer, le lien est seulement non vérifié) ?
 *
 * Par défaut `true` (« recherche ») si le champ est absent : les traces
 * persistées AVANT cette distinction n'avaient cette étape que lorsque
 * `@web` avait effectivement tourné — le défaut reflète leur sens d'origine,
 * sans migration nécessaire.
 */
export function verifieeContreRecherche(etape: EtapeTrace): boolean {
  return etape.verifiees_contre !== 'aucune_source'
}

/**
 * Résumé d'une trace, pour l'en-tête REPLIÉ du panneau (« Recherche web :
 * 5 résultats en 0,8 s » / « Recherche web : échec » / « Citation hors
 * sources » / « Lien non vérifié »).
 *
 * Vérifie EXPLICITEMENT qu'une étape de recherche existe avant de parler de
 * recherche — sinon une trace ne portant que `citations_invalides`
 * afficherait « Recherche web… » alors qu'aucune requête n'est jamais
 * partie, ce qui est exactement le genre d'affirmation trompeuse que la
 * trace existe pour éliminer.
 */
export function resumeTrace(etapes: EtapeTrace[]): string {
  const aUneEtapeDeRecherche = etapes.some(e => e.etape !== 'citations_invalides')
  if (!aUneEtapeDeRecherche) {
    const citations = etapes.find(e => e.etape === 'citations_invalides')
    if (!citations) return ''  // ne devrait pas arriver : trace vide → pas de panneau
    return verifieeContreRecherche(citations) ? 'Citation hors sources' : 'Lien non vérifié'
  }

  const derniere = [...etapes].reverse().find(
    e => e.etape === 'recherche_resultats' || e.etape === 'recherche_erreur'
  )
  if (!derniere) return 'Recherche web…'
  if (derniere.etape === 'recherche_erreur') return 'Recherche web : échec'
  const nombre = Number(derniere.nombre) || 0
  if (nombre === 0) return 'Recherche web : aucun résultat'
  const ms = Number(derniere.ms) || 0
  const secondes = (ms / 1000).toFixed(1).replace('.', ',')
  return `Recherche web : ${nombre} résultat${nombre > 1 ? 's' : ''} en ${secondes} s`
}

/**
 * Libellé du badge affiché À CÔTÉ du résumé, ou `null` si aucun n'est dû.
 *
 * Le badge n'existe qu'EN PLUS d'un résumé de recherche : sans recherche,
 * `resumeTrace` dit déjà « Citation hors sources »/« Lien non vérifié » — un
 * badge identique juste à côté serait un doublon, pas une information
 * supplémentaire.
 */
export function libelleBadgeCitations(etapes: EtapeTrace[]): string | null {
  const etapeCitations = etapes.find(e => e.etape === 'citations_invalides')
  const aUneEtapeDeRecherche = etapes.some(e => e.etape !== 'citations_invalides')
  if (!etapeCitations || !aUneEtapeDeRecherche) return null
  return verifieeContreRecherche(etapeCitations) ? 'citation hors sources' : 'lien non vérifié'
}
