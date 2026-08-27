import { describe, it, expect } from 'vitest'
import { metaAffichable, NON_DISPONIBLE } from './metaMessage'

/**
 * Le sujet de ce fichier tient en une phrase : **« pas de modèle par nature » et
 * « on ne sait pas » ne doivent pas s'afficher pareil.**
 *
 * Un message tapé par l'utilisateur n'est produit par aucun modèle — il n'y a
 * rien à afficher, et rien n'est perdu. Une réponse écrite avant l'ajout de ces
 * champs, elle, a bien eu un modèle : on ne le connaît simplement plus. Fondre
 * les deux en un seul « non disponible » ferait croire à une donnée perdue là où
 * il n'y a rien à perdre.
 *
 * Et le piège qu'aucun test ne doit laisser passer : **combler un champ absent
 * depuis le `modèle` de la conversation.** Celui-ci dit le DERNIER modèle
 * utilisé, il a pu changer plusieurs fois, et s'en servir attribuerait des
 * réponses à un modèle qui ne les a pas produites.
 */

describe('metaAffichable — horodatage', () => {
  it('découpe un horodatage du serveur en date et heure', () => {
    const m = metaAffichable('2026-08-27T14:03:11', 'qwen2.5:7b', false)
    expect(m.date).toBe('27/08/2026')
    expect(m.heure).toBe('14:03:11')
  })

  it('accepte un horodatage sans les secondes', () => {
    const m = metaAffichable('2026-08-27T14:03', undefined, true)
    expect(m.heure).toBe('14:03')
  })

  it('rend l’heure TELLE QUE le serveur l’a écrite, sans décalage', () => {
    // Le backend écrit `datetime.now().isoformat()` : une heure LOCALE sans
    // fuseau. La passer par `new Date()` puis `toLocaleString()` peut la
    // décaler ; le découpage manuel ne le peut pas. Ce test échouerait au
    // premier retour à une conversion par `Date`.
    expect(metaAffichable('2026-01-01T00:30:00', undefined, true).heure).toBe('00:30:00')
    expect(metaAffichable('2026-07-01T23:45:00', undefined, true).heure).toBe('23:45:00')
  })

  it('dit « non disponible » quand le champ manque — sans rien deviner', () => {
    const m = metaAffichable(undefined, undefined, false)
    expect(m.date).toBe(NON_DISPONIBLE)
    expect(m.heure).toBe(NON_DISPONIBLE)
  })

  it('dit « non disponible » sur un horodatage illisible plutôt que d’afficher NaN', () => {
    for (const mauvais of ['', 'hier', '2026-08-27', 'null']) {
      const m = metaAffichable(mauvais, undefined, false)
      expect(m.date).toBe(NON_DISPONIBLE)
      expect(m.heure).toBe(NON_DISPONIBLE)
    }
  })
})

describe('metaAffichable — modèle, et LA distinction', () => {
  it('un message utilisateur n’a PAS de ligne modèle', () => {
    expect(metaAffichable('2026-08-27T14:03:11', undefined, true).modele).toBeNull()
  })

  it('un message utilisateur n’en a pas non plus si un modèle traîne dans les données', () => {
    // Ceinture : même si un jour un `modèle` se retrouvait posé sur un message
    // utilisateur, l'affirmation « ce texte vient de ce modèle » resterait fausse.
    expect(metaAffichable('2026-08-27T14:03:11', 'qwen2.5:7b', true).modele).toBeNull()
  })

  it('une réponse récente affiche son modèle', () => {
    expect(metaAffichable('2026-08-27T14:03:11', 'gemini-2.0-flash', false).modele)
      .toBe('gemini-2.0-flash')
  })

  it('une réponse ANCIENNE dit « non disponible » — et non rien', () => {
    // L'autre moitié de la distinction : ici un modèle a bien existé, on ne le
    // connaît plus. Rendre `null` la ferait passer pour un message utilisateur.
    expect(metaAffichable(undefined, undefined, false).modele).toBe(NON_DISPONIBLE)
  })

  it('les deux absences ne se ressemblent pas', () => {
    const utilisateur = metaAffichable(undefined, undefined, true)
    const ancienneReponse = metaAffichable(undefined, undefined, false)
    expect(utilisateur.modele).not.toBe(ancienneReponse.modele)
  })
})
