import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

import Chat from './Component'

/**
 * `@image` et l'etat d'analyse vision (chantier 2026-09-07).
 *
 * Meme patron de test que `Component.comparaison.test.tsx` — double minimal de
 * `WebSocket`, `fetch` stube par table d'URL — et meme discipline : le nom du
 * champ envoye au backend n'est PAS deductible de celui de la commande, donc
 * il est affirme explicitement (`vision_override`, un booleen, cf. le piege
 * documente pour `@cours` qui part en `rag_override: 'all'`).
 *
 * Ce que ces tests protegent, et qui n'est visible ni pour `tsc -b` ni pour
 * eslint : que la REANALYSE d'une image reste un geste EXPLICITE. Le premier
 * declenchement est automatique cote backend (image attachee + pas d'analyse) ;
 * refaire l'analyse coute 6 a 26 s par image (mesure, cf. CLAUDE.md §3.3 bis)
 * et ne doit donc jamais partir sans que l'utilisateur l'ait tape.
 */

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

function tableSaine() {
  return {
    '/pair': { corps: { token: 'jeton-de-test' } },
    '/context': { corps: { 'modèle_actif': 'qwen2.5:7b', strict_mode: false, 'instruction_générale': '' } },
    '/rag/files': { corps: { files: [] } },
    '/rag/capabilities': { corps: { 'état': 'prêt', disponible: true, message: '', cause: '', 'taille_estimée_mo': 0 } },
    '/models': {
      corps: {
        local: [{ id: 'ollama:a', nom: 'Modèle A', disponible: true }],
        local_npu: [], cloud: {}, fournisseurs: {}, recommandations: {},
      },
    },
    '/voice/capabilities': {
      corps: {
        transcription: { disponible: false, manquants: [], raison: '' },
        'synthèse': { disponible: false, manquants: [], raison: '' },
      },
    },
    '/modules': { corps: { modules: [] } },
  }
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
  close() { /* jamais appelé dans ces tests */ }
}

async function rendreEtConnecter() {
  const rendu = render(<Chat />)
  await act(async () => { await Promise.resolve() })
  const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
  await act(async () => { ws.onopen?.() })
  return { rendu, ws }
}

async function envoyerTexte(texte: string) {
  const zone = screen.getByPlaceholderText('Message...')
  await act(async () => { fireEvent.change(zone, { target: { value: texte } }) })
  await act(async () => { fireEvent.keyDown(zone, { key: 'Enter' }) })
}

function envoyer(ws: FakeWebSocket, data: Record<string, unknown>) {
  ws.onmessage?.({ data: JSON.stringify(data) })
}

// jsdom n'implémente pas `scrollIntoView` — chaque message poussé en pose un.
HTMLElement.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  cleanup()
  localStorage.clear()
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

describe('Chat — override @image', () => {
  it('@image : le message porte vision_override et la commande est retirée du texte', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await envoyerTexte('@image que vaut le rapport AB/AC ?')

    const envoye = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(envoye.vision_override).toBe(true)
    // La question part NETTOYÉE : c'est elle qui devient le prompt du modèle
    // vision côté backend, et « @image » dedans n'aiderait personne.
    expect(envoye.content).toBe('que vaut le rapport AB/AC ?')
  })

  it('sans @image, aucun vision_override ne part — la réanalyse reste explicite', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await envoyerTexte('et le théorème utilisé, lequel est-ce ?')

    const envoye = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(envoye.vision_override).toBeUndefined()
  })

  it('@image se combine avec @strict, dans les deux ordres', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await envoyerTexte('@image @strict lis la formule')
    let envoye = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(envoye.vision_override).toBe(true)
    expect(envoye.strict_override).toBe(true)
    expect(envoye.content).toBe('lis la formule')

    await envoyerTexte('@strict @image lis la formule')
    envoye = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(envoye.vision_override).toBe(true)
    expect(envoye.strict_override).toBe(true)
  })
})

describe('Chat — état de l’analyse vision', () => {
  it('une analyse en cours est annoncée, avec le nom du fichier', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await envoyerTexte('que vaut AB/AC ?')
    await act(async () => {
      envoyer(ws, {
        type: 'vision_analyse', 'état': 'en_cours', fichier: 'enonce.png',
        index: 1, total: 1, reste: 0,
      })
    })

    // Le silence est le mode d'échec qu'on ferme : 6 à 26 s avant le premier
    // token, sur une question parfaitement normale.
    expect(screen.getByText(/lecture de l’image enonce\.png/)).toBeTruthy()
  })

  it('une analyse terminée n’affiche plus rien', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await envoyerTexte('que vaut AB/AC ?')
    await act(async () => {
      envoyer(ws, { type: 'vision_analyse', 'état': 'en_cours', fichier: 'enonce.png', index: 1, total: 1, reste: 0 })
      envoyer(ws, { type: 'vision_analyse', 'état': 'terminée', fichier: 'enonce.png', index: 1, total: 1, reste: 0 })
    })

    expect(screen.queryByText(/lecture de l’image/)).toBeNull()
  })

  it('un échec SURVIT au done : la réponse a été construite sans l’image', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await envoyerTexte('que vaut AB/AC ?')
    await act(async () => {
      envoyer(ws, { type: 'vision_analyse', 'état': 'échec', fichier: 'enonce.png', index: 1, total: 1, reste: 0 })
      envoyer(ws, { type: 'token', content: 'je ne peux pas voir' })
      envoyer(ws, { type: 'done' })
    })

    // Effacer cet avertissement avec le curseur de frappe le rendrait
    // invisible : il parle de la réponse qu'on vient de lire.
    expect(screen.getByText(/image non lue/)).toBeTruthy()
  })

  it('un échec ne survit PAS à la question suivante', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await envoyerTexte('que vaut AB/AC ?')
    await act(async () => {
      envoyer(ws, { type: 'vision_analyse', 'état': 'échec', fichier: 'enonce.png', index: 1, total: 1, reste: 0 })
      envoyer(ws, { type: 'done' })
    })
    await envoyerTexte('autre question')

    expect(screen.queryByText(/image non lue/)).toBeNull()
  })

  it('un événement dont le corps est incomplet ne casse pas le rendu', async () => {
    // Frontière `.json()` du websocket : le backend peut évoluer, et un champ
    // absent arrive `undefined`. `String(undefined)` et `Number(undefined)`
    // sont bornés ici plutôt que laissés atteindre un `.length` au rendu
    // suivant (CLAUDE.md §8).
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await envoyerTexte('que vaut AB/AC ?')
    await act(async () => { envoyer(ws, { type: 'vision_analyse' }) })

    expect(screen.getByText(/lecture de l’image/)).toBeTruthy()
  })
})
