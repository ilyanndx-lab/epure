import { describe, it, expect } from 'vitest'
import { budgetRechercheApprofondie, infoBulleRechercheApprofondie } from './rechercheApprofondie'

/**
 * L'info-bulle doit citer le budget RÉELLEMENT configuré — et, faute de le
 * connaître, n'en citer aucun plutôt que l'ancien « 4 » en dur.
 */
describe('budgetRechercheApprofondie', () => {
  it('lit le budget configuré dans GET /context', () => {
    const ctx = { tool_calling: { enabled: true, skills: { recherche_approfondie: { enabled: false, budget: 7 } } } }
    expect(budgetRechercheApprofondie(ctx)).toBe(7)
  })

  it('null sur une forme inattendue — jamais un nombre inventé', () => {
    for (const ctx of [null, undefined, 'x', {}, { tool_calling: {} }, { tool_calling: { skills: [] } },
      { tool_calling: { skills: { recherche_approfondie: { budget: '4' } } } },
      { tool_calling: { skills: { recherche_approfondie: { budget: 0 } } } },
      { tool_calling: { skills: { recherche_approfondie: { budget: 2.5 } } } }]) {
      expect(budgetRechercheApprofondie(ctx)).toBeNull()
    }
  })
})

describe('infoBulleRechercheApprofondie', () => {
  it('cite le budget configuré, pas « 4 » en dur', () => {
    expect(infoBulleRechercheApprofondie(8)).toContain("jusqu'à 8 requêtes")
    expect(infoBulleRechercheApprofondie(1)).toContain("jusqu'à 1 requête)")
  })

  it('aucun nombre quand le budget est inconnu', () => {
    const texte = infoBulleRechercheApprofondie(null)
    expect(texte).toContain('plusieurs requêtes')
    expect(texte).not.toMatch(/\d/)
  })
})
