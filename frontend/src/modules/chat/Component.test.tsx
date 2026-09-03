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
