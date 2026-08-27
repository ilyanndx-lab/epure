import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { chargerConversations, creerConversation, entree } from './conversations'

/**
 * Les frontières `.json()` des conversations, éprouvées sur la FORME des
 * réponses — pas seulement sur le cas nominal.
 *
 * C'est la raison d'être de la suite vitest de ce dépôt (CLAUDE.md) : un `as`
 * posé sur un `r.json()` est une affirmation, pas une vérification. Le
 * compilateur croit l'annotation ; le serveur, lui, répond parfois un corps
 * d'erreur — le 401 avant appairage, le 404 sur une route absente, le
 * `{"detail": …, "type": …}` du gestionnaire d'exceptions. Le champ annoncé est
 * alors `undefined`, le `.catch()` ne voit rien puisque `r.json()` a réussi, et
 * la faute n'apparaît qu'au rendu suivant, sur un `.length` ou un `.map()`, dans
 * un chunk minifié où la trace ne nomme même pas la ligne.
 *
 * Ces cas ne sont pas théoriques : c'est exactement ce qui est arrivé au panneau
 * fichiers du module Docs dans un paquet livré.
 */

const vraiFetch = globalThis.fetch

function reponse(corps: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => corps,
  } as unknown as Response
}

beforeEach(() => {
  localStorage.setItem('epure.apiToken', 'jeton-de-test')
})

afterEach(() => {
  globalThis.fetch = vraiFetch
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('chargerConversations', () => {
  it('rend la liste sur une réponse nominale', async () => {
    globalThis.fetch = vi.fn(async () => reponse({
      conversations: [{ id: 'a', titre: 'Thermo', n_messages: 4 }],
      total: 1,
    })) as unknown as typeof fetch

    const rendu = await chargerConversations()
    expect(rendu).toHaveLength(1)
    expect(rendu[0].id).toBe('a')
    expect(rendu[0].titre).toBe('Thermo')
  })

  it('rend [] sur le corps d’un 401 avant appairage', async () => {
    globalThis.fetch = vi.fn(async () =>
      reponse({ detail: 'Token manquant' }, 401)) as unknown as typeof fetch
    await expect(chargerConversations()).resolves.toEqual([])
  })

  it('rend [] sur le corps du gestionnaire d’exceptions (500)', async () => {
    globalThis.fetch = vi.fn(async () =>
      reponse({ detail: 'Erreur interne du serveur', type: 'ValueError' }, 500)) as unknown as typeof fetch
    await expect(chargerConversations()).resolves.toEqual([])
  })

  it('rend [] quand `conversations` n’est pas un tableau', async () => {
    // 200 avec un corps inattendu : le cas que `Array.isArray` attrape et que
    // `?? []` laisserait passer.
    for (const mauvais of [null, undefined, 'texte', 42, { a: 1 }]) {
      globalThis.fetch = vi.fn(async () =>
        reponse({ conversations: mauvais })) as unknown as typeof fetch
      await expect(chargerConversations()).resolves.toEqual([])
    }
  })

  it('rend [] si le réseau lève', async () => {
    globalThis.fetch = vi.fn(async () => { throw new Error('backend éteint') }) as unknown as typeof fetch
    await expect(chargerConversations()).resolves.toEqual([])
  })

  it('écarte les entrées sans identifiant plutôt que de les afficher', async () => {
    globalThis.fetch = vi.fn(async () => reponse({
      conversations: [{ titre: 'sans id' }, { id: 'b', titre: 'ok' }],
    })) as unknown as typeof fetch

    const rendu = await chargerConversations()
    expect(rendu.map(c => c.id)).toEqual(['b'])
  })
})

describe('entree', () => {
  it('comble les champs absents sans produire "undefined" à l’écran', () => {
    const e = entree({ id: 'x' })
    expect(e.titre).toBe('')
    expect(e.apercu).toBe('')
    expect(e.n_messages).toBe(0)
    expect(e.n_fichiers).toBe(0)
  })

  it('résiste à un corps qui n’est pas un objet', () => {
    for (const mauvais of [null, undefined, 'texte', 42]) {
      expect(() => entree(mauvais)).not.toThrow()
      expect(entree(mauvais).id).toBe('')
    }
  })

  it('ignore un `n_messages` du mauvais type', () => {
    // Un backend qui renverrait une chaîne ferait afficher « 12 messages » à
    // partir de "12", mais casserait toute comparaison numérique.
    expect(entree({ id: 'x', n_messages: '12' }).n_messages).toBe(0)
  })
})

describe('creerConversation', () => {
  it('rend l’identifiant créé', async () => {
    globalThis.fetch = vi.fn(async () =>
      reponse({ id: 'neuve', titre: '' })) as unknown as typeof fetch
    await expect(creerConversation()).resolves.toBe('neuve')
  })

  it('lève sur un échec — l’utilisateur a cliqué, il doit le savoir', async () => {
    globalThis.fetch = vi.fn(async () =>
      reponse({ detail: 'non' }, 503)) as unknown as typeof fetch
    await expect(creerConversation()).rejects.toThrow()
  })

  it('rend "" plutôt que "undefined" si le corps n’a pas d’`id`', async () => {
    // 200 sans `id` : pathologique, mais `texte()` évite qu'un
    // `conversation_id: "undefined"` parte ensuite dans chaque message.
    globalThis.fetch = vi.fn(async () => reponse({})) as unknown as typeof fetch
    await expect(creerConversation()).resolves.toBe('')
  })
})
