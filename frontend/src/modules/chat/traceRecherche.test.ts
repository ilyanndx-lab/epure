import { describe, it, expect } from 'vitest'
import { aAtteintLimiteRecherches, aUneRechercheAbandonnee, detailBudgetLecture, etapesDe, libelleBadgeAbandon, libelleBadgeCitations, libelleBadgeLecture, libelleBadgeLimite, libelleRetraitOutils, resumeTrace, verifieeContreRecherche, type EtapeTrace } from './traceRecherche'

/**
 * Le sujet de ce fichier tient en deux phrases :
 *
 * 1. **Une trace sans étape de recherche ne doit JAMAIS annoncer une
 *    recherche.** `trace_recherche` peut désormais ne contenir que
 *    `citations_invalides` (@web est un override rare ; la validation des
 *    citations tourne à chaque tour, avec ou sans recherche) — le résumé
 *    doit le refléter, pas dire « Recherche web… » par défaut.
 * 2. **« vérifié contre une vraie recherche » et « rien à comparer » ne
 *    doivent pas porter le même libellé.** Un badge qui crie au loup à
 *    chaque URL de mémoire non vérifiée finirait ignoré.
 */

const RECHERCHE_RESULTATS: EtapeTrace = {
  etape: 'recherche_resultats', nombre: 5, moteur: 'ddg-html', ms: 812, resultats: [],
}
const RECHERCHE_ERREUR: EtapeTrace = { etape: 'recherche_erreur', message: 'HTTP 403' }
const CITATIONS_RECHERCHE: EtapeTrace = {
  etape: 'citations_invalides', rangs: [7], urls: [], verifiees_contre: 'recherche',
}
const CITATIONS_AUCUNE_SOURCE: EtapeTrace = {
  etape: 'citations_invalides', rangs: [], urls: ['https://invente.example/'],
  verifiees_contre: 'aucune_source',
}
const CITATIONS_SANS_CHAMP: EtapeTrace = { etape: 'citations_invalides', rangs: [7], urls: [] }

describe('etapesDe', () => {
  it('écarte une entrée sans discriminant `etape` exploitable', () => {
    expect(etapesDe([{ rang: 1 }, { etape: 'recherche_erreur' }])).toEqual([
      { etape: 'recherche_erreur' },
    ])
  })

  it('rend un tableau vide sur une valeur qui n’en est pas un', () => {
    expect(etapesDe(undefined)).toEqual([])
    expect(etapesDe('rien')).toEqual([])
    expect(etapesDe(null)).toEqual([])
  })
})

describe('verifieeContreRecherche', () => {
  it('vrai quand verifiees_contre vaut "recherche"', () => {
    expect(verifieeContreRecherche(CITATIONS_RECHERCHE)).toBe(true)
  })

  it('faux quand verifiees_contre vaut "aucune_source"', () => {
    expect(verifieeContreRecherche(CITATIONS_AUCUNE_SOURCE)).toBe(false)
  })

  it('vrai par défaut quand le champ est absent (rétrocompatibilité)', () => {
    // Les traces persistées AVANT cette distinction n'avaient cette étape
    // que lorsque @web avait tourné — le défaut reflète leur sens d'origine.
    expect(verifieeContreRecherche(CITATIONS_SANS_CHAMP)).toBe(true)
  })
})

describe('resumeTrace — ne jamais annoncer une recherche qui n’a pas eu lieu', () => {
  it('trace réduite à citations_invalides (verifiees_contre="recherche") : "Citation hors sources"', () => {
    expect(resumeTrace([CITATIONS_RECHERCHE])).toBe('Citation hors sources')
  })

  it('trace réduite à citations_invalides (verifiees_contre="aucune_source") : "Lien non vérifié"', () => {
    expect(resumeTrace([CITATIONS_AUCUNE_SOURCE])).toBe('Lien non vérifié')
  })

  it('ne dit jamais "Recherche web" quand aucune étape de recherche n’existe', () => {
    const resume = resumeTrace([CITATIONS_AUCUNE_SOURCE])
    expect(resume).not.toMatch(/recherche web/i)
  })

  it('résumé normal avec des résultats de recherche', () => {
    expect(resumeTrace([
      { etape: 'recherche_debut', requete: 'python', moteur: 'ddg-instant' },
      RECHERCHE_RESULTATS,
    ])).toBe('Recherche web : 5 résultats en 0,8 s')
  })

  it('singulier à 1 résultat', () => {
    expect(resumeTrace([{ ...RECHERCHE_RESULTATS, nombre: 1 }])).toBe('Recherche web : 1 résultat en 0,8 s')
  })

  it('aucun résultat, mais une recherche a bien eu lieu', () => {
    expect(resumeTrace([{ ...RECHERCHE_RESULTATS, nombre: 0 }])).toBe('Recherche web : aucun résultat')
  })

  it('échec de recherche', () => {
    expect(resumeTrace([RECHERCHE_ERREUR])).toBe('Recherche web : échec')
  })

  it('trace vide : rien à résumer (ne devrait de toute façon jamais s’afficher — pas de panneau sans étape)', () => {
    expect(resumeTrace([])).toBe('')
  })
})

describe('libelleBadgeCitations — le badge n’est dû qu’EN PLUS d’un résumé de recherche', () => {
  it('null quand la trace ne contient QUE citations_invalides (déjà dit par resumeTrace)', () => {
    expect(libelleBadgeCitations([CITATIONS_RECHERCHE])).toBeNull()
    expect(libelleBadgeCitations([CITATIONS_AUCUNE_SOURCE])).toBeNull()
  })

  it('"citation hors sources" quand une recherche a eu lieu et l’URL n’en fait pas partie', () => {
    expect(libelleBadgeCitations([RECHERCHE_RESULTATS, CITATIONS_RECHERCHE])).toBe('citation hors sources')
  })

  it('"lien non vérifié" quand une recherche a eu lieu mais l’anomalie ne lui est pas imputable', () => {
    expect(libelleBadgeCitations([RECHERCHE_RESULTATS, CITATIONS_AUCUNE_SOURCE])).toBe('lien non vérifié')
  })

  it('null sans étape citations_invalides', () => {
    expect(libelleBadgeCitations([RECHERCHE_RESULTATS])).toBeNull()
  })

  it('null sur une trace vide', () => {
    expect(libelleBadgeCitations([])).toBeNull()
  })
})

describe('recherche abandonnée (tool_call_abandonne) — visible sans déplier la trace', () => {
  const ABANDON: EtapeTrace = { etape: 'tool_call_abandonne', raison: 'arguments_invalides', outils: ['web_search'] }
  const TOOL_CALL: EtapeTrace = { etape: 'tool_call_web_search', requete: 'météo Lyon' }

  it('résumé « Recherche abandonnée », jamais « Recherche web… » quand rien n’a abouti', () => {
    expect(resumeTrace([ABANDON])).toBe('Recherche abandonnée')
    expect(resumeTrace([ABANDON, CITATIONS_AUCUNE_SOURCE])).toBe('Recherche abandonnée')
  })

  it('pas de badge en double quand le résumé le dit déjà', () => {
    expect(libelleBadgeAbandon([ABANDON])).toBeNull()
  })

  it('badge quand une première recherche a abouti puis une seconde a été abandonnée', () => {
    const etapes = [TOOL_CALL, RECHERCHE_RESULTATS, ABANDON]
    expect(resumeTrace(etapes)).toBe('Recherche web : 5 résultats en 0,8 s')
    expect(libelleBadgeAbandon(etapes)).toBe('recherche abandonnée')
  })

  it('aucun badge ni changement de résumé sans abandon', () => {
    expect(aUneRechercheAbandonnee([TOOL_CALL, RECHERCHE_RESULTATS])).toBe(false)
    expect(libelleBadgeAbandon([TOOL_CALL, RECHERCHE_RESULTATS])).toBeNull()
    expect(resumeTrace([RECHERCHE_ERREUR])).toBe('Recherche web : échec')
  })

  it('survit à la normalisation d’une trace persistée', () => {
    expect(aUneRechercheAbandonnee(etapesDe([{ etape: 'tool_call_abandonne', raison: 'flux_interrompu' }]))).toBe(true)
  })
})

describe('plafond d’allers-retours (tool_call_plafond_atteint) — même traitement que l’abandon', () => {
  const PLAFOND: EtapeTrace = { etape: 'tool_call_plafond_atteint', rounds: 2 }
  const TOOL_CALL: EtapeTrace = { etape: 'tool_call_web_search', requete: 'météo Lyon' }

  it('résumé « Limite de recherches atteinte », jamais « Recherche web… »', () => {
    expect(resumeTrace([PLAFOND])).toBe('Limite de recherches atteinte')
    expect(libelleBadgeLimite([PLAFOND])).toBeNull()
  })

  it('badge quand une recherche du même tour a abouti', () => {
    const etapes = [TOOL_CALL, RECHERCHE_RESULTATS, PLAFOND]
    expect(resumeTrace(etapes)).toBe('Recherche web : 5 résultats en 0,8 s')
    expect(libelleBadgeLimite(etapes)).toBe('limite de recherches atteinte')
  })

  it('rien sans l’étape', () => {
    expect(aAtteintLimiteRecherches([TOOL_CALL, RECHERCHE_RESULTATS])).toBe(false)
    expect(libelleBadgeLimite([TOOL_CALL, RECHERCHE_RESULTATS])).toBeNull()
  })
})

describe('lecture directe d’URL (lecture_url) — ni « Recherche web », ni échec silencieux', () => {
  const LECTURE_OK: EtapeTrace = {
    etape: 'lecture_url', url: 'https://site.example/a', statut: 'succès', ms: 420, passages_retenus: 3,
  }
  const LECTURE_OK_1: EtapeTrace = { ...LECTURE_OK, url: 'https://site.example/b', passages_retenus: 1 }
  const LECTURE_LOGIN: EtapeTrace = {
    etape: 'lecture_url', url: 'https://site.example/prive', statut: 'échec', ms: 300,
    passages_retenus: 0, raison: 'page protégée par une connexion (HTTP 401)',
  }
  const LECTURE_REFUSEE: EtapeTrace = {
    etape: 'lecture_url', url: 'http://127.0.0.1:8000/', statut: 'refusée', ms: 0,
    passages_retenus: 0, raison: 'adresse locale ou privée refusée (127.0.0.1)',
  }
  const RECLASSEMENT: EtapeTrace = { etape: 'reclassement_indisponible', raison: 'embedding indisponible' }

  it('page lue : nombre de passages, jamais « Recherche web »', () => {
    expect(resumeTrace([LECTURE_OK])).toBe('Page lue : 3 passages')
    expect(resumeTrace([LECTURE_OK_1])).toBe('Page lue : 1 passage')
    expect(resumeTrace([RECLASSEMENT, LECTURE_OK])).toBe('Page lue : 3 passages')
    expect(resumeTrace([LECTURE_OK])).not.toMatch(/recherche web/i)
  })

  it('plusieurs pages lues', () => {
    expect(resumeTrace([LECTURE_OK, LECTURE_OK_1])).toBe('2 pages lues')
  })

  it('échec : la raison est dans le résumé replié, sans déplier', () => {
    expect(resumeTrace([LECTURE_LOGIN])).toBe('Page non lue : page protégée par une connexion (HTTP 401)')
    expect(resumeTrace([LECTURE_OK, LECTURE_LOGIN])).toBe('Page non lue : page protégée par une connexion (HTTP 401)')
    expect(resumeTrace([LECTURE_REFUSEE])).toBe('Page non lue : adresse locale ou privée refusée (127.0.0.1)')
  })

  it('échec sans raison (trace abîmée) : libellé générique, jamais vide', () => {
    expect(resumeTrace([{ etape: 'lecture_url', statut: 'échec' }])).toBe('Page non lue')
  })

  it('badge « page non lue » à côté d’un résumé de recherche', () => {
    expect(libelleBadgeLecture([RECHERCHE_RESULTATS, LECTURE_LOGIN])).toBe('page non lue')
    expect(resumeTrace([RECHERCHE_RESULTATS, LECTURE_LOGIN])).toBe('Recherche web : 5 résultats en 0,8 s')
  })

  it('pas de badge en double quand le résumé le dit déjà, ni quand tout a été lu', () => {
    expect(libelleBadgeLecture([LECTURE_LOGIN])).toBeNull()
    expect(libelleBadgeLecture([RECHERCHE_RESULTATS, LECTURE_OK])).toBeNull()
    expect(libelleBadgeLecture([RECHERCHE_RESULTATS])).toBeNull()
  })

  it('le retrait des outils (outils_retires) n’est pas une recherche', () => {
    const RETRAIT: EtapeTrace = { etape: 'outils_retires', raison: 'lecture_url', outils: ['web_search'] }
    expect(resumeTrace([LECTURE_OK, RETRAIT])).toBe('Page lue : 3 passages')
    expect(resumeTrace([LECTURE_LOGIN, RETRAIT])).toBe('Page non lue : page protégée par une connexion (HTTP 401)')
  })

  it('le badge citations reste dû quand la seule autre étape est une lecture', () => {
    expect(libelleBadgeCitations([LECTURE_OK, CITATIONS_AUCUNE_SOURCE])).toBe('lien non vérifié')
  })
})

describe('libelleRetraitOutils — la raison du retrait dite telle quelle', () => {
  it('lecture d’URL', () => {
    expect(libelleRetraitOutils({ etape: 'outils_retires', raison: 'lecture_url', outils: ['web_search'] }))
      .toBe('Outils du modèle retirés pour ce tour (web_search) : une page web a été lue, et son texte ne doit pas pouvoir déclencher de recherche.')
  })

  it('contenu web injecté (@web, déclenchement automatique, résultat d’outil)', () => {
    expect(libelleRetraitOutils({ etape: 'outils_retires', raison: 'contenu_web', outils: ['web_search', 'history_search'] }))
      .toBe('Outils du modèle retirés pour ce tour (web_search, history_search) : des résultats web ont été injectés, et leur texte ne doit pas pouvoir déclencher de recherche.')
  })

  it('raison inconnue (trace future) : libellé générique, jamais vide', () => {
    expect(libelleRetraitOutils({ etape: 'outils_retires' }))
      .toBe('Outils du modèle retirés pour ce tour (aucun).')
  })
})

describe('detailBudgetLecture', () => {
  // Une page lue dit COMBIEN elle a pris, sur QUEL budget, et d'où vient ce
  // budget — sans quoi « 3 passages retenus » ne permet pas de savoir si la
  // réponse a pu tomber sous la coupe.
  it('contexte chargé connu : taille, budget, contexte et sa source', () => {
    expect(detailBudgetLecture({
      etape: 'lecture_url', statut: 'succès', selection: 'similarite', caracteres: 6950,
      budget_page: 7168, budget_caracteres: 7168, contexte_charge: 8192,
      source_contexte: 'lmstudio /api/v1/models',
    })).toBe('6950 car. sur 7168 — contexte chargé 8192 tokens (lmstudio /api/v1/models)')
  })

  it('budget partagé entre plusieurs pages : la part de la page et le total', () => {
    expect(detailBudgetLecture({
      etape: 'lecture_url', statut: 'succès', selection: 'similarite', caracteres: 3500,
      budget_page: 3584, budget_caracteres: 7168, contexte_charge: 8192,
      source_contexte: 'ollama /api/ps',
    })).toBe('3500 car. sur 3584 (total 7168) — contexte chargé 8192 tokens (ollama /api/ps)')
  })

  it('contexte inconnu : le plancher est dit comme tel', () => {
    expect(detailBudgetLecture({
      etape: 'lecture_url', statut: 'succès', selection: 'similarite', caracteres: 3900,
      budget_page: 4000, budget_caracteres: 4000, contexte_charge: null, source_contexte: null,
    })).toBe('3900 car. sur 4000 — contexte chargé inconnu, plancher')
  })

  it("repli sur l'ordre de la page : dit, pas tu", () => {
    expect(detailBudgetLecture({
      etape: 'lecture_url', statut: 'succès', selection: 'ordre_page', caracteres: 3900,
      budget_page: 4000, budget_caracteres: 4000, contexte_charge: null, source_contexte: null,
    })).toBe('3900 car. sur 4000 — contexte chargé inconnu, plancher — début de page, sans reclassement')
  })

  it("trace d'avant ce budget (historique) : rien à ajouter", () => {
    expect(detailBudgetLecture({
      etape: 'lecture_url', statut: 'succès', passages_retenus: 3, ms: 900,
    })).toBeNull()
  })
})
