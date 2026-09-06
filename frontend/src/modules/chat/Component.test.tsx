import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

import Chat from './Component'

/**
 * Défilement automatique du chat — le bug rapporté.
 *
 * AVANT : `useEffect(() => bottomRef.current?.scrollIntoView(...), [messages])`
 * était INCONDITIONNEL — chaque token reçu pendant un streaming ramenait la
 * vue en bas, même si l'utilisateur avait remonté manuellement pour relire un
 * message plus haut. Corrigé en ne défilant que si l'utilisateur était déjà
 * proche du bas (suivi en continu par un écouteur `scroll`, cf.
 * `Component.tsx`), sauf pour l'envoi d'un nouveau message — l'action qui
 * justifie de le ramener de force.
 *
 * Ces tests ne portent QUE sur ce comportement : le reste du backend est
 * neutralisé par des réponses 500/404 par défaut (comme
 * `ModuleBar.test.tsx`, « reste rendu quand TOUT le backend répond 500 ») —
 * ModuleBar (rendu par Chat) tolère déjà ce cas sans planter.
 */

/** Table URL → réponse. Défaut 500 : toute route oubliée se comporte comme
 * le pire cas réel, jamais comme un silence — même convention que
 * `ModuleBar.test.tsx`. */
function poserFetch(table: Record<string, { status?: number; corps: unknown }> = {}) {
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : String(input)
    const cle = Object.keys(table).find(k => url.includes(k))
    const { status = 200, corps } = cle ? table[cle] : { corps: { detail: 'Erreur' }, status: 500 }
    return new Response(JSON.stringify(corps), {
      status, headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', impl)
  return impl
}

/** Table minimale pour que `ModuleBar` (rendu par `Chat`) et l'appairage se
 * passent sans bruit — le contenu exact ne compte pas pour ces tests. */
function tableSaine() {
  return {
    '/pair': { corps: { token: 'jeton-de-test' } },
    '/context': { corps: { 'modèle_actif': 'qwen2.5:7b', strict_mode: false, 'instruction_générale': '' } },
    '/rag/files': { corps: { files: [] } },
    '/rag/capabilities': { corps: { 'état': 'prêt', disponible: true, message: '', cause: '', 'taille_estimée_mo': 0 } },
    '/models': { corps: { local: [], local_npu: [], cloud: {}, fournisseurs: {}, recommandations: {} } },
    '/voice/capabilities': {
      corps: {
        transcription: { disponible: false, manquants: [], raison: '' },
        'synthèse': { disponible: false, manquants: [], raison: '' },
      },
    },
    '/modules': { corps: { modules: [] } },
  }
}

/** Double minimal de `WebSocket` : capture l'instance pour que le test pilote
 * `onopen`/`onmessage` à la main, sans jamais toucher au réseau. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  readonly sent: string[] = []
  readonly url: string
  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }
  send(data: string) { this.sent.push(data) }
  close() { /* jamais appelé dans ces tests */ }
}

/** Rend `Chat`, connecte le websocket factice, rend le message `token`
 * possible en poussant une première réponse assistant vide n'est pas
 * nécessaire : `data.type === 'token'` crée le message assistant lui-même
 * s'il n'existe pas encore (cf. `Component.tsx`). */
async function rendreEtConnecter() {
  const rendu = render(<Chat />)
  await act(async () => { await Promise.resolve() })
  const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
  await act(async () => { ws.onopen?.() })
  return { rendu, ws }
}

function envoyerToken(ws: FakeWebSocket, texte: string) {
  ws.onmessage?.({ data: JSON.stringify({ type: 'token', content: texte }) })
}

/** Le conteneur défilant des MESSAGES — `ConversationList` a elle aussi un
 * `.overflow-y-auto` (le panneau des fils), rendu AVANT dans le DOM : il faut
 * le chercher sous `<main>`, pas au premier `.overflow-y-auto` du document. */
function conteneur(): HTMLElement {
  const el = document.querySelector('main .overflow-y-auto')
  if (!el) throw new Error('conteneur de messages introuvable')
  return el as HTMLElement
}

/** Positionne le conteneur — `scrollHeight`/`clientHeight` sont des
 * getters en jsdom (toujours 0) : il faut les redéfinir pour simuler une
 * vraie position de scroll. */
function positionner(el: HTMLElement, { scrollHeight, scrollTop, clientHeight }: {
  scrollHeight: number; scrollTop: number; clientHeight: number
}) {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
  Object.defineProperty(el, 'scrollTop', { value: scrollTop, configurable: true })
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

describe('Chat — défilement automatique', () => {
  it('utilisateur en bas + nouveau token → défilement déclenché', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const scrollIntoView = vi.fn()
    vi.stubGlobal('HTMLElement', HTMLElement)
    HTMLElement.prototype.scrollIntoView = scrollIntoView

    const { ws } = await rendreEtConnecter()
    const el = conteneur()
    // « En bas » : aucun écart entre le contenu et le bas visible.
    positionner(el, { scrollHeight: 200, scrollTop: 100, clientHeight: 100 })
    fireEvent.scroll(el)

    scrollIntoView.mockClear()
    await act(async () => { envoyerToken(ws, 'Bonjour') })

    expect(scrollIntoView).toHaveBeenCalled()
  })

  it('utilisateur remonté + nouveau token → aucun défilement', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const scrollIntoView = vi.fn()
    HTMLElement.prototype.scrollIntoView = scrollIntoView

    const { ws } = await rendreEtConnecter()
    const el = conteneur()
    // Remonté : un grand écart entre le bas du contenu et la position visible.
    positionner(el, { scrollHeight: 2000, scrollTop: 0, clientHeight: 200 })
    fireEvent.scroll(el)

    scrollIntoView.mockClear()
    await act(async () => { envoyerToken(ws, 'Bonjour') })

    expect(scrollIntoView).not.toHaveBeenCalled()
  })

  it('nouveau message envoyé par l’utilisateur → défilement déclenché même remonté', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const scrollIntoView = vi.fn()
    HTMLElement.prototype.scrollIntoView = scrollIntoView

    await rendreEtConnecter()
    const el = conteneur()
    positionner(el, { scrollHeight: 2000, scrollTop: 0, clientHeight: 200 })
    fireEvent.scroll(el)

    scrollIntoView.mockClear()

    const zone = screen.getByPlaceholderText('Message...')
    fireEvent.change(zone, { target: { value: 'Une nouvelle question' } })
    const bouton = screen.getByTitle('Envoyer')
    await act(async () => { fireEvent.click(bouton) })

    expect(scrollIntoView).toHaveBeenCalled()
  })
})


/**
 * Indicateur « le modèle LM Studio charge » — pendant l'attente du premier token.
 *
 * LM Studio charge son modèle à la demande, et ce chargement se compte en
 * dizaines de secondes (12,1 s mesurées pour 3 Go, 119,4 s pour 17,7 Go sur le
 * poste de dev, 2026-09-06). Sans signal, l'utilisateur ne voit qu'un curseur
 * clignotant.
 *
 * **Ce n'est PAS un pourcentage, et ces tests sont là pour que ça le reste.**
 * LM Studio n'expose aucun nombre sur son API HTTP : le corps de sa route de
 * chargement refuse `stream` en 400 (schéma fermé — l'absence est prouvée, pas
 * seulement constatée), la réponse arrive en un bloc à la fin, et aucune route
 * d'état n'existe ailleurs. Détail des mesures dans
 * `backend/core/models.py:etat_modele_lmstudio`.
 *
 * Le reste éprouve la FORME de la réponse, au sens de CLAUDE.md §4 : un `as`
 * sur un `r.json()` est une affirmation, pas une vérification, et cette route
 * peut répondre un corps d'erreur (500 avant appairage, 404 sur une instance
 * qui ne l'a pas). Les trois cas « on ne sait pas » — `null`, corps d'erreur,
 * champ absent — doivent laisser l'affichage tel quel, jamais l'allumer.
 */
describe('Chat — chargement LM Studio', () => {
  const LIBELLE = 'chargement du modèle en cours…'

  /** Envoie un message : `streaming` passe à vrai et le dernier message reste
   * de rôle `user`, donc on est exactement dans la fenêtre d'attente du
   * premier token — celle où l'indicateur a un sens. */
  async function envoyerMessage() {
    const zone = screen.getByPlaceholderText('Message...')
    fireEvent.change(zone, { target: { value: 'Une question' } })
    await act(async () => { fireEvent.click(screen.getByTitle('Envoyer')) })
  }

  async function rendreAvec(reponse: { status?: number; corps: unknown } | null) {
    // **La clé de CETTE route doit venir AVANT `/models`.** `poserFetch`
    // retient la PREMIÈRE clé dont l'URL contient le texte, et
    // `/models/lmstudio/chargement` contient `/models` : posée après, elle est
    // masquée par le catalogue de modèles, et les tests passeraient en
    // mesurant autre chose. Payé une fois en écrivant ce fichier.
    const table = {
      ...(reponse ? { '/models/lmstudio/chargement': reponse } : {}),
      ...tableSaine(),
    } as Record<string, { status?: number; corps: unknown }>
    // `null` = on ne pose RIEN pour cette route : elle retombe sur le 500 par
    // défaut de `poserFetch`, c'est-à-dire le corps d'erreur réel du
    // gestionnaire d'exceptions, pas un silence.
    poserFetch(table)
    vi.stubGlobal('WebSocket', FakeWebSocket)
    HTMLElement.prototype.scrollIntoView = vi.fn()
    await rendreEtConnecter()
    await envoyerMessage()
  }

  it('chargement: true → le libellé s’affiche', async () => {
    await rendreAvec({ corps: { modele: 'mistralai/ministral-3-3b', chargement: true } })
    expect(await screen.findByText(LIBELLE)).toBeTruthy()
  })

  /** Laisse le sondage se résoudre AVANT de conclure à une absence : sans
   * cette attente, le test passerait même si le libellé s'allumait juste
   * après — il mesurerait la lenteur du `fetch`, pas le comportement. */
  async function laisserSonder() {
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
  }

  it('chargement: false → aucun libellé', async () => {
    await rendreAvec({ corps: { modele: 'mistralai/ministral-3-3b', chargement: false } })
    await laisserSonder()
    expect(screen.queryByText(LIBELLE)).toBeNull()
  })

  it('chargement: null (LM Studio injoignable) → aucun libellé, aucune erreur', async () => {
    await rendreAvec({ corps: { modele: 'm', chargement: null } })
    await laisserSonder()
    expect(screen.queryByText(LIBELLE)).toBeNull()
  })

  it('corps d’ERREUR (500) → aucun libellé et le chat reste rendu', async () => {
    // Le cas qui a coûté un panneau mort ailleurs dans ce dépôt : `r.json()`
    // RÉUSSIT sur un corps d'erreur, donc le `.catch()` ne voit rien et le
    // champ annoncé vaut `undefined`.
    await rendreAvec(null)
    await laisserSonder()
    expect(screen.queryByText(LIBELLE)).toBeNull()
    expect(screen.getByPlaceholderText('Message...')).toBeTruthy()
  })

  it('champ `chargement` ABSENT d’un corps 200 → aucun libellé', async () => {
    await rendreAvec({ corps: { modele: 'm' } })
    await laisserSonder()
    expect(screen.queryByText(LIBELLE)).toBeNull()
  })

  it('un `null` n’ÉTEINT pas un `true` déjà affiché', async () => {
    // La règle qui évite le clignotement : LM Studio rend un 500 transitoire à
    // l'instant précis où un chargement démarre (observé une fois sur 79
    // sondes). Traiter ce « on ne sait pas » comme un `false` ferait
    // disparaître l'indicateur en plein chargement.
    let corps: unknown = { modele: 'm', chargement: true }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/models/lmstudio/chargement')) {
        return new Response(JSON.stringify(corps), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      const table = tableSaine() as Record<string, { status?: number; corps: unknown }>
      const cle = Object.keys(table).find(k => url.includes(k))
      const { status = 200, corps: c } = cle ? table[cle] : { corps: { detail: 'Erreur' }, status: 500 }
      return new Response(JSON.stringify(c), {
        status, headers: { 'Content-Type': 'application/json' },
      })
    }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    HTMLElement.prototype.scrollIntoView = vi.fn()
    await rendreEtConnecter()
    await envoyerMessage()
    expect(await screen.findByText(LIBELLE)).toBeTruthy()

    // La sonde suivante ne sait plus rien : l'affichage ne doit pas bouger.
    // Attente en temps REEL plutot qu'en horloge simulee : `vi.useFakeTimers()`
    // fige aussi les promesses que `findByText` attend, et le remede
    // couterait plus de mecanique que la seconde qu'il economise.
    corps = { modele: 'm', chargement: null }
    await act(async () => { await new Promise(r => setTimeout(r, 1400)) })
    expect(screen.getByText(LIBELLE)).toBeTruthy()
  })

  it('aucun POURCENTAGE n’est affiché, quoi que réponde le backend', async () => {
    // Le garde-fou du chantier. Même si un champ numérique apparaissait un jour
    // dans la réponse, il ne devrait JAMAIS atteindre l'écran : il aurait été
    // fabriqué, LM Studio n'en fournissant aucun.
    await rendreAvec({ corps: { modele: 'm', chargement: true, progression: 42 } })
    expect(await screen.findByText(LIBELLE)).toBeTruthy()
    expect(screen.queryByText(/42\s*%/)).toBeNull()
    expect(screen.queryByText(/%/)).toBeNull()
  })
})
