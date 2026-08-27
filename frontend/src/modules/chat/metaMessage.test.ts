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

describe('metaAffichable — modèle : la valeur, et le LIBELLÉ qui la qualifie', () => {
  it('une réponse affiche son modèle sous le libellé « modèle »', () => {
    const m = metaAffichable('2026-08-27T14:03:11', 'gemini-2.0-flash', false)
    expect(m.modele).toBe('gemini-2.0-flash')
    expect(m.libelleModele).toBe('modèle')
  })

  it('un message utilisateur affiche le modèle sous « envoyé à »', () => {
    // Ce que le libellé évite : écrire « modèle : qwen2.5 » sous un texte tapé
    // par l'utilisateur laisserait entendre que ce texte vient du modèle.
    // « envoyé à » dit exactement ce qui s'est passé.
    const m = metaAffichable('2026-08-27T14:03:11', 'qwen2.5:7b', true)
    expect(m.modele).toBe('qwen2.5:7b')
    expect(m.libelleModele).toBe('envoyé à')
  })

  it('seul le libellé dépend du rôle, jamais la valeur', () => {
    const question = metaAffichable('2026-08-27T14:03:11', 'qwen2.5:7b', true)
    const reponse = metaAffichable('2026-08-27T14:03:12', 'qwen2.5:7b', false)
    expect(question.modele).toBe(reponse.modele)
    expect(question.libelleModele).not.toBe(reponse.libelleModele)
  })

  it('un message d’avant ce champ dit « non disponible », des deux côtés', () => {
    // Rétrocompatibilité : rien n'est deviné, et surtout pas depuis le `modèle`
    // de la conversation, qui n'est que le DERNIER utilisé.
    expect(metaAffichable(undefined, undefined, true).modele).toBe(NON_DISPONIBLE)
    expect(metaAffichable(undefined, undefined, false).modele).toBe(NON_DISPONIBLE)
  })

  it('une chaîne vide vaut « non disponible », pas une ligne vide', () => {
    expect(metaAffichable('2026-08-27T14:03:11', '', false).modele).toBe(NON_DISPONIBLE)
  })
})
