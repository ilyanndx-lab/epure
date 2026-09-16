import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

import Chat from './Component'

/**
 * Fuite de texte entre conversations — LE bug corrigé ici.
 *
 * Une seule connexion `/ws/chat` sert TOUTES les conversations d'un onglet
 * (voulu, cf. CLAUDE.md, pas remis en cause). Avant ce correctif, rien dans le
 * protocole n'identifiait à quelle conversation appartenait un événement de
 * streaming : basculer d'un fil A (qui génère encore) vers un fil B pendant
 * la génération laissait les `token` de A continuer à s'accumuler dans
 * l'écran de B, puisque le handler mutait `messages` sans jamais vérifier
 * qu'un événement entrant appartenait au fil AFFICHÉ.
 *
 * Le correctif est bilatéral : chaque événement porte désormais
 * `conversation_id` (backend/modules/chat/router.py, verrouillé par
 * `backend/test_chat_conversation_id_trames.py`) et le client compare cet
 * identifiant à celui du fil affiché AVANT toute mutation d'état
 * (`Component.tsx`, `conversationIdRef`). Ce fichier verrouille le côté
 * client : aucune fuite visible, aucune troncature au retour sur un fil qui a
 * continué de générer pendant l'absence, et des stats qui ne mentent jamais
 * sur ce qui est affiché.
 *
 * Même patron de test que `Component.comparaison.test.tsx` : un double
 * minimal de `WebSocket`, un `fetch` stubé par table d'URL.
 */

function poserFetch(table: Record<string, { status?: number; corps: unknown }>) {
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

/**
 * Table de base + les deux conversations du test. **Ordre significatif** :
 * `poserFetch` retient la PREMIÈRE clé dont l'URL contient le texte —
 * `/chat/conversations/conv-a` doit donc être déclarée AVANT `/chat/conversations`
 * (qui la contient comme sous-chaîne), sans quoi la route générique
 * masquerait la route spécifique. Même piège que documenté dans
 * `Component.test.tsx` pour `/models/lmstudio/chargement`.
 */
function tableSaine(extra: Record<string, { status?: number; corps: unknown }> = {}) {
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
    ...extra,
    '/chat/conversations': {
      corps: {
        conversations: [
          { id: 'conv-a', titre: 'Conversation A', date: '', apercu: '', n_messages: 0, 'modifiée': '', n_fichiers: 0 },
          { id: 'conv-b', titre: 'Conversation B', date: '', apercu: '', n_messages: 0, 'modifiée': '', n_fichiers: 0 },
        ],
        total: 2,
      },
    },
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

/** Laisse une chaîne de promesses (fetch → .json() → setState) se dérouler. */
async function attendre() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

async function ouvrir(titre: string) {
  const bouton = screen.getByTitle(titre)
  await act(async () => { fireEvent.click(bouton) })
  await attendre()
}

HTMLElement.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  cleanup()
  localStorage.clear()
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

describe('Chat — étanchéité entre conversations', () => {
  it('un token de la conversation NON affichée ne fuit jamais dans le fil affiché', async () => {
    localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
    poserFetch(tableSaine({
      '/chat/conversations/conv-a': { corps: { messages: [] } },
      '/chat/conversations/conv-b': { corps: { messages: [{ role: 'assistant', content: 'Message existant B' }] } },
    }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()
    await attendre() // charge conv-a (vide) au montage

    await act(async () => { envoyer(ws, { type: 'token', content: 'Bonjour de A', conversation_id: 'conv-a' }) })
    expect(screen.getByText('Bonjour de A')).toBeTruthy()

    await ouvrir('Conversation B')
    expect(screen.queryByText('Bonjour de A')).toBeNull()
    expect(screen.getByText('Message existant B')).toBeTruthy()

    // Le tour de A continue de générer alors que B est affiché : ces tokens
    // ne doivent JAMAIS apparaître, ni maintenant ni plus tard.
    await act(async () => { envoyer(ws, { type: 'token', content: ' FUITE', conversation_id: 'conv-a' }) })
    expect(screen.queryByText(/FUITE/)).toBeNull()
    expect(screen.getByText('Message existant B')).toBeTruthy()

    // Un token de B, lui, doit s'appliquer normalement.
    await act(async () => { envoyer(ws, { type: 'token', content: ' — suite', conversation_id: 'conv-b' }) })
    expect(screen.getByText('Message existant B — suite')).toBeTruthy()
  })

  it('retour AVANT le `done` : le texte affiché n\'est pas tronqué, et aucune stat fausse n\'est attachée', async () => {
    localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
    // Mutable : la réponse de conv-a change entre le retour (avant `done`,
    // seul le message UTILISATEUR est écrit — cf. router.py : il est ajouté
    // au tout début du tour, bien avant que la réponse ne commence à
    // streamer) et la fin réelle du tour (après `done`, la réponse complète
    // est sur le disque) — exactement ce qui se passe côté vrai backend.
    const reponseConvA: { corps: unknown } = { corps: { messages: [{ role: 'user', content: 'question' }] } }
    poserFetch(tableSaine({
      '/chat/conversations/conv-a': reponseConvA,
      '/chat/conversations/conv-b': { corps: { messages: [] } },
    }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()
    await attendre()

    // Début de la réponse, reçu pendant qu'on affiche encore A.
    await act(async () => { envoyer(ws, { type: 'token', content: 'Bon', conversation_id: 'conv-a' }) })
    expect(screen.getByText('Bon')).toBeTruthy()

    // Bascule vers B AVANT que le tour de A ne se termine.
    await ouvrir('Conversation B')
    expect(screen.queryByText('Bon')).toBeNull()

    // La génération de A continue pendant l'absence : ces tokens sont jetés
    // (cf. le test précédent) mais la conversation est marquée « suspecte ».
    await act(async () => { envoyer(ws, { type: 'token', content: 'jour', conversation_id: 'conv-a' }) })

    // Retour sur A AVANT le `done` — le disque n'a toujours que la version
    // vide (rien écrit avant la fin du tour).
    await ouvrir('Conversation A')
    expect(screen.queryByText(/Bon|jour/)).toBeNull()

    // La suite du flux, reçue maintenant qu'on est revenu sur A, s'applique
    // normalement — mais elle ne porte que la QUEUE, pas le total.
    await act(async () => { envoyer(ws, { type: 'token', content: ' voilà', conversation_id: 'conv-a' }) })
    expect(screen.getByText('voilà')).toBeTruthy()

    // Le tour se termine : le disque a maintenant la réponse COMPLÈTE.
    reponseConvA.corps = {
      messages: [
        { role: 'user', content: 'question' },
        {
          role: 'assistant', content: 'Bonjour voilà',
          'horodatage': '2026-01-01T00:00:00', 'modèle': 'qwen2.5:7b',
        },
      ],
    }
    await act(async () => {
      envoyer(ws, {
        type: 'done', conversation_id: 'conv-a',
        'horodatage': '2026-01-01T00:00:00', 'modèle': 'qwen2.5:7b',
        sources: [], trace_recherche: [],
      })
    })
    await attendre()

    // Le texte affiché est le texte COMPLET relu sur le disque, jamais la
    // seule queue reçue après le retour.
    expect(screen.getByText('Bonjour voilà')).toBeTruthy()
    expect(screen.queryByText('voilà')).toBeNull()
    // Aucune stat n'est attachée : elle ne compterait que les tokens reçus
    // après le retour, pas ceux générés pendant l'absence — un chiffre faux
    // est pire qu'aucun chiffre.
    expect(screen.queryByText(/out tokens/)).toBeNull()
  })

  it('retour APRÈS le `done` : la relecture normale (bascule de fil) suffit déjà', async () => {
    localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
    const reponseConvA: { corps: unknown } = { corps: { messages: [] } }
    poserFetch(tableSaine({
      '/chat/conversations/conv-a': reponseConvA,
      '/chat/conversations/conv-b': { corps: { messages: [] } },
    }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()
    await attendre()

    await act(async () => { envoyer(ws, { type: 'token', content: 'Bon', conversation_id: 'conv-a' }) })
    await ouvrir('Conversation B')

    // Le tour de A se termine PENDANT qu'on regarde B : `done` est filtré,
    // mais le disque, lui, est à jour dès maintenant.
    reponseConvA.corps = {
      messages: [{ role: 'assistant', content: 'Bonjour complet', 'horodatage': '2026-01-01T00:00:00', 'modèle': 'qwen2.5:7b' }],
    }
    await act(async () => {
      envoyer(ws, {
        type: 'done', conversation_id: 'conv-a',
        'horodatage': '2026-01-01T00:00:00', 'modèle': 'qwen2.5:7b', sources: [], trace_recherche: [],
      })
    })

    await ouvrir('Conversation A')
    expect(screen.getByText('Bonjour complet')).toBeTruthy()
  })

  it('une annonce de création tardive ne ramène pas l\'écran sur le fil qu\'on vient de quitter', async () => {
    // Aucune conversation affichée au départ (`conversationIdRef` part à '') :
    // c'est le cas normal d'un premier message envoyé sans identifiant, cf.
    // `sendUserText` — le serveur crée alors la conversation et l'annonce par
    // `{"type": "conversation"}`.
    poserFetch(tableSaine({
      '/chat/conversations/conv-b': { corps: { messages: [{ role: 'assistant', content: 'Message existant B' }] } },
    }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()
    await attendre()

    // L'utilisateur bascule vers un fil EXISTANT avant que le serveur ait eu
    // le temps de répondre à ce premier message.
    await ouvrir('Conversation B')
    expect(screen.getByText('Message existant B')).toBeTruthy()

    // L'annonce de création, tardive, arrive pour la conversation créée par
    // ce premier message : elle ne doit PAS ramener l'écran dessus.
    await act(async () => { envoyer(ws, { type: 'conversation', id: 'conv-neuve', conversation_id: 'conv-neuve' }) })

    // Toujours affiché : B. Un token de la conversation neuve n'apparaît
    // jamais ; un token de B, lui, s'applique toujours normalement — la
    // preuve que c'est bien B qui reste le fil affiché.
    await act(async () => { envoyer(ws, { type: 'token', content: 'FUITE-NEUVE', conversation_id: 'conv-neuve' }) })
    expect(screen.queryByText(/FUITE-NEUVE/)).toBeNull()
    await act(async () => { envoyer(ws, { type: 'token', content: ' — suite', conversation_id: 'conv-b' }) })
    expect(screen.getByText('Message existant B — suite')).toBeTruthy()
  })
})
