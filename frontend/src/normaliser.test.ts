import { describe, expect, it } from 'vitest'

import { dico, liste, modelesDisponibles, texte } from './normaliser'

/**
 * `modelesDisponibles` face à un `GET /models` qui n'a pas la forme annoncée.
 *
 * Motif direct : `settings/Component.tsx` aplatissait `/models` avec sa propre
 * logique (`...(d.local ?? [])`, `...(d.cloud?.rapide ?? [])`, …) au lieu de
 * passer par cette fonction. `?? []` ne rattrape que `null`/`undefined` — une
 * chaîne ou un objet à la place d'un tableau le traverse tel quel, puis casse
 * au premier spread ou `.map()`. `Array.isArray` (via `liste()`) est le seul
 * garde qui couvre aussi ces cas-là ; ce fichier fixe le comportement que la
 * migration de settings/Component.tsx vers `modelesDisponibles` doit garder.
 */
describe('modelesDisponibles', () => {
  it('aplatit et déduplique par id, catégories nominales', () => {
    const r = modelesDisponibles({
      local: [{ id: 'a', nom: 'A', disponible: true }],
      local_npu: [],
      cloud: {
        rapide: [{ id: 'b', nom: 'B', disponible: true }],
        puissant: [{ id: 'a', nom: 'A (doublon)', disponible: false }],
        long_contexte: [],
      },
    })
    expect(r).toEqual([
      { id: 'a', nom: 'A', disponible: true },
      { id: 'b', nom: 'B', disponible: true },
    ])
  })

  it("tient quand `cloud` n'est pas un objet (une chaîne, itérable caractère par caractère)", () => {
    expect(() => modelesDisponibles({ local: [], cloud: 'oups' })).not.toThrow()
    expect(modelesDisponibles({ local: [], cloud: 'oups' })).toEqual([])
  })

  it("tient quand une catégorie (`local`) n'est pas un tableau", () => {
    expect(modelesDisponibles({ local: 'oups', cloud: {} })).toEqual([])
  })

  it('rend une liste vide sur un corps entièrement inattendu (500, 401, 404)', () => {
    expect(modelesDisponibles({ detail: 'Erreur interne du serveur', type: 'ImportError' })).toEqual([])
    expect(modelesDisponibles(null)).toEqual([])
    expect(modelesDisponibles(undefined)).toEqual([])
    expect(modelesDisponibles('oups')).toEqual([])
  })

  it('ignore une entrée sans id, et retombe sur id pour un nom manquant', () => {
    const r = modelesDisponibles({
      local: [{ nom: 'Sans id', disponible: true }, { id: 'x', disponible: true }],
    })
    expect(r).toEqual([{ id: 'x', nom: 'x', disponible: true }])
  })
})

describe('liste / dico / texte', () => {
  it('liste() ne rend un tableau que si Array.isArray est vrai', () => {
    expect(liste('oups')).toEqual([])
    expect(liste(null)).toEqual([])
    expect(liste([1, 2])).toEqual([1, 2])
  })

  it("dico() refuse un tableau — 'x' in [] ne veut rien dire ici", () => {
    expect(dico([1, 2])).toEqual({})
    expect(dico(null)).toEqual({})
    expect(dico({ a: true })).toEqual({ a: true })
  })

  it('texte() ne rend jamais "undefined"', () => {
    expect(texte(undefined)).toBe('')
    expect(texte(42)).toBe('')
    expect(texte('ok')).toBe('ok')
  })
})
