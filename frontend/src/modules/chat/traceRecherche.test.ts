import { describe, it, expect } from 'vitest'
import { etapesDe, libelleBadgeCitations, resumeTrace, verifieeContreRecherche, type EtapeTrace } from './traceRecherche'

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
