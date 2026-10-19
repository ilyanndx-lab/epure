import { describe, it, expect } from 'vitest'
import { capacitesRaisonnement, normaliserModeRaisonnement } from './raisonnement'

describe('mode de raisonnement : trois états, ancien booléen → auto', () => {
  it('les trois états passent tels quels', () => {
    for (const etat of ['auto', 'active', 'desactive'] as const) {
      expect(normaliserModeRaisonnement(etat)).toBe(etat)
    }
  })

  it('tout le reste devient auto, y compris true/false (migration)', () => {
    for (const ancien of [true, false, null, undefined, '', 'on', 1]) {
      expect(normaliserModeRaisonnement(ancien)).toBe('auto')
    }
  })
})

describe('capacitesRaisonnement', () => {
  it('LM Studio affiche sa réflexion et sait la couper (mesuré, #76) : plus « budget seul »', () => {
    expect(capacitesRaisonnement('lmstudio')).toEqual({ nonSupporte: false, budgetSeul: false })
  })

  it('les autres cas inchangés', () => {
    expect(capacitesRaisonnement('gemini').nonSupporte).toBe(true)
    expect(capacitesRaisonnement('ollama').budgetSeul).toBe(false)
    expect(capacitesRaisonnement('flm').budgetSeul).toBe(false)
    expect(capacitesRaisonnement('mistral').budgetSeul).toBe(true)
  })
})
