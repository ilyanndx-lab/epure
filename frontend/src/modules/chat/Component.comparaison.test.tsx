import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

import Chat from './Component'

/**
 * Comparaison multi-modèles (§ tâche 2026-09-03) — protocole websocket décrit
 * dans `backend/modules/chat/router.py` (`compare_models` en entrée,
 * `compare_token`/`compare_reasoning`/`compare_stats`/`compare_error`/
 * `compare_done`/`compare_all_done` en sortie, `compare_choix` pour résoudre).
 *
 * Même patron de test que `Component.test.tsx` (défilement automatique) :
 * un double minimal de `WebSocket`, un `fetch` stubé par table d'URL, et le
 * reste du backend neutralisé par des réponses saines par défaut.
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

/** Quatre modèles disponibles, pour exercer le plafond de 3 et le choix. */
function tableSaine() {
  return {
    '/pair': { corps: { token: 'jeton-de-test' } },
    '/context': { corps: { 'modèle_actif': 'qwen2.5:7b', strict_mode: false, 'instruction_générale': '' } },
    '/rag/files': { corps: { files: [] } },
    '/rag/capabilities': { corps: { 'état': 'prêt', disponible: true, message: '', cause: '', 'taille_estimée_mo': 0 } },
    '/models': {
      corps: {
        local: [
          { id: 'ollama:a', nom: 'Modèle A', disponible: true },
          { id: 'ollama:b', nom: 'Modèle B', disponible: true },
          { id: 'ollama:c', nom: 'Modèle C', disponible: true },
          { id: 'ollama:d', nom: 'Modèle D', disponible: true },
        ],
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

function envoyer(ws: FakeWebSocket, data: Record<string, unknown>) {
  ws.onmessage?.({ data: JSON.stringify(data) })
}

/** Ouvre le menu de comparaison et coche les modèles nommés, dans l'ordre. */
async function activerComparaison(noms: string[]) {
  const bouton = screen.getByTitle('Comparer plusieurs modèles côte à côte')
  await act(async () => { fireEvent.click(bouton) })
  for (const nom of noms) {
    const checkbox = screen.getByLabelText(nom)
    await act(async () => { fireEvent.click(checkbox) })
  }
}

async function envoyerTexte(texte: string) {
  const zone = screen.getByPlaceholderText('Message...')
  await act(async () => { fireEvent.change(zone, { target: { value: texte } }) })
  await act(async () => { fireEvent.keyDown(zone, { key: 'Enter' }) })
}

// jsdom n'implémente pas `scrollIntoView` — chaque message poussé en pose un
// (cf. l'effet de défilement automatique), donc requis même quand ce n'est
// pas ce que le test vérifie. Même patron que `Component.test.tsx`.
HTMLElement.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  cleanup()
  localStorage.clear()
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

describe('Chat — sélection des modèles à comparer', () => {
  it('coche 2 modèles → comparaison activable ; un 4e est refusé au-delà de 3', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    await rendreEtConnecter()

    await activerComparaison(['Modèle A', 'Modèle B', 'Modèle C'])

    // Le 4e est désactivé (case grisée) — pas juste refusé après coup.
    const checkboxD = screen.getByLabelText('Modèle D') as HTMLInputElement
    expect(checkboxD.disabled).toBe(true)
    await act(async () => { fireEvent.click(checkboxD) })
    expect(checkboxD.checked).toBe(false)
  })

  it('un seul modèle coché → le message part sans compare_models', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await activerComparaison(['Modèle A'])
    await envoyerTexte('Bonjour')

    const dernier = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(dernier.compare_models).toBeUndefined()
    expect(dernier.content).toBe('Bonjour')
  })
})

describe('Chat — comparaison en cours', () => {
  it('accumule le texte de chaque modèle indépendamment, dans un ordre entrelacé', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await activerComparaison(['Modèle A', 'Modèle B'])
    await envoyerTexte('Compare ces deux modèles')

    const envoye = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(envoye.compare_models).toEqual(['ollama:a', 'ollama:b'])

    await act(async () => {
      envoyer(ws, { type: 'compare_token', model: 'ollama:a', content: 'Réponse ' })
      envoyer(ws, { type: 'compare_token', model: 'ollama:b', content: 'Autre ' })
      envoyer(ws, { type: 'compare_token', model: 'ollama:a', content: 'A' })
      envoyer(ws, { type: 'compare_token', model: 'ollama:b', content: 'B' })
    })

    expect(() => screen.getByText('Réponse A')).not.toThrow()
    expect(() => screen.getByText('Autre B')).not.toThrow()
  })

  it('« Garder cette réponse » reste désactivé tant que compare_done du modèle n\'est pas reçu', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await activerComparaison(['Modèle A', 'Modèle B'])
    await envoyerTexte('Compare')

    await act(async () => {
      envoyer(ws, { type: 'compare_token', model: 'ollama:a', content: 'Réponse A' })
      envoyer(ws, { type: 'compare_token', model: 'ollama:b', content: 'Réponse B' })
    })

    const boutons = screen.getAllByRole('button', { name: 'Garder cette réponse' }) as HTMLButtonElement[]
    expect(boutons).toHaveLength(2)
    boutons.forEach(b => expect(b.disabled).toBe(true))

    await act(async () => {
      envoyer(ws, { type: 'compare_done', model: 'ollama:a' })
    })

    const [boutonA, boutonB] = screen.getAllByRole('button', { name: 'Garder cette réponse' }) as HTMLButtonElement[]
    expect(boutonA.disabled).toBe(false)
    expect(boutonB.disabled).toBe(true)
  })

  it('un modèle qui termine en erreur, sans texte, ne rend jamais son bouton actif', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await activerComparaison(['Modèle A', 'Modèle B'])
    await envoyerTexte('Compare')

    await act(async () => {
      envoyer(ws, { type: 'compare_error', model: 'ollama:a', content: 'Timeout' })
      envoyer(ws, { type: 'compare_done', model: 'ollama:a' })
      envoyer(ws, { type: 'compare_token', model: 'ollama:b', content: 'Réponse B' })
      envoyer(ws, { type: 'compare_done', model: 'ollama:b' })
    })

    const boutons = screen.getAllByRole('button', { name: 'Garder cette réponse' }) as HTMLButtonElement[]
    expect(boutons[0].disabled).toBe(true)
    expect(boutons[1].disabled).toBe(false)
  })
})

describe('Chat — résolution de la comparaison (compare_choix → done)', () => {
  it('un choix résolu pousse EXACTEMENT un message assistant et fait disparaître le panneau', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await activerComparaison(['Modèle A', 'Modèle B'])
    await envoyerTexte('Compare')

    await act(async () => {
      envoyer(ws, { type: 'compare_token', model: 'ollama:a', content: 'Réponse A' })
      envoyer(ws, { type: 'compare_token', model: 'ollama:b', content: 'Réponse B' })
      envoyer(ws, { type: 'compare_done', model: 'ollama:a' })
      envoyer(ws, { type: 'compare_done', model: 'ollama:b' })
      envoyer(ws, { type: 'compare_all_done' })
    })

    const boutonA = screen.getAllByRole('button', { name: 'Garder cette réponse' })[0]
    await act(async () => { fireEvent.click(boutonA) })

    // Double clic immédiat : ne doit pas envoyer un second `compare_choix`.
    await act(async () => { fireEvent.click(boutonA) })
    const choix = ws.sent.filter(s => JSON.parse(s).type === 'compare_choix')
    expect(choix).toHaveLength(1)
    expect(JSON.parse(choix[0])).toMatchObject({ type: 'compare_choix', model: 'ollama:a' })

    await act(async () => {
      envoyer(ws, {
        type: 'done', 'horodatage': '2026-09-04T10:00:00', 'modèle': 'ollama:a',
        sources: [], trace_recherche: [],
      })
    })

    // Le panneau de comparaison a disparu.
    expect(screen.queryByRole('button', { name: 'Garder cette réponse' })).toBeNull()
    // Exactement un nouveau message assistant, avec le bon contenu.
    const messagesAssistant = screen.getAllByText('Réponse A')
    expect(messagesAssistant).toHaveLength(1)
  })

  it('une réponse compare_choix périmée (error) laisse le panneau réessayable', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await activerComparaison(['Modèle A', 'Modèle B'])
    await envoyerTexte('Compare')

    await act(async () => {
      envoyer(ws, { type: 'compare_token', model: 'ollama:a', content: 'Réponse A' })
      envoyer(ws, { type: 'compare_token', model: 'ollama:b', content: 'Réponse B' })
      envoyer(ws, { type: 'compare_done', model: 'ollama:a' })
      envoyer(ws, { type: 'compare_done', model: 'ollama:b' })
    })

    const boutonA = screen.getAllByRole('button', { name: 'Garder cette réponse' })[0] as HTMLButtonElement
    await act(async () => { fireEvent.click(boutonA) })
    expect(boutonA.disabled).toBe(true)

    await act(async () => {
      envoyer(ws, { type: 'error', content: 'Comparaison introuvable ou déjà résolue pour ce modèle.' })
    })

    // Les boutons sont réactivés — l'utilisateur peut choisir l'autre réponse.
    const boutons = screen.getAllByRole('button', { name: 'Garder cette réponse' }) as HTMLButtonElement[]
    expect(boutons[0].disabled).toBe(false)
    expect(boutons[1].disabled).toBe(false)

    await act(async () => { fireEvent.click(boutons[1]) })
    const choix = ws.sent.filter(s => JSON.parse(s).type === 'compare_choix')
    expect(choix).toHaveLength(2)
    expect(JSON.parse(choix[1]).model).toBe('ollama:b')
  })
})

describe('Chat — @web/@cours/@strict en mode comparaison', () => {
  it('@web reste fonctionnel : le message porte compare_models ET web_search_override', async () => {
    poserFetch(tableSaine())
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()

    await activerComparaison(['Modèle A', 'Modèle B'])
    await envoyerTexte('@web quelle est la météo')

    const envoye = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(envoye.compare_models).toEqual(['ollama:a', 'ollama:b'])
    expect(envoye.web_search_override).toBe(true)
    expect(envoye.content).toBe('quelle est la météo')
  })
})
