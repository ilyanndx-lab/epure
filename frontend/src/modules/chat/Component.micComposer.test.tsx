import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'

import Chat from './Component'

/**
 * Bouton micro relogé dans l'îlot du composer (à droite d'"Adaptatif"),
 * porté par portail depuis `ModuleBar` — itération 4 du header de
 * conversation, même principe que le chip modèle.
 *
 * Ce test vit SEUL dans son propre fichier : `voix.ts` mémorise son verdict
 * (`transcription disponible`) au niveau du MODULE, pas du composant, la
 * première fois que `chargerVoix()` se déclenche (`demandeEnCours`), pour
 * toute la durée du fichier de test — y compris via `localStorage`, que
 * `afterEach` vide mais qui ne réinitialise pas l'état déjà chargé en
 * mémoire. Un autre test du micro DÉSACTIVÉ, dans le même fichier, y
 * verrait donc toujours son propre fixture ignoré.
 */

function poserFetch(table: Record<string, { status?: number; corps: unknown }>) {
  const impl = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
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

function tableSaine(extra: Record<string, { status?: number; corps: unknown }> = {}) {
  return {
    '/pair': { corps: { token: 'jeton-de-test' } },
    '/context': { corps: { 'modèle_actif': 'qwen2.5:7b', strict_mode: false, 'instruction_générale': '' } },
    '/rag/files': { corps: { files: [] } },
    '/rag/capabilities': { corps: { 'état': 'prêt', disponible: true, message: '', cause: '', 'taille_estimée_mo': 0 } },
    '/models': { corps: { local: [], local_npu: [], cloud: {}, fournisseurs: {}, recommandations: {} } },
    '/voice/capabilities': {
      corps: {
        transcription: { disponible: true, manquants: [], raison: '' },
        'synthèse': { disponible: false, manquants: [], raison: '' },
      },
    },
    '/modules': { corps: { modules: [] } },
    '/chat/conversations': { corps: { conversations: [], total: 0 } },
    ...extra,
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
  vi.stubGlobal('WebSocket', FakeWebSocket)
  const rendu = render(<Chat />)
  await act(async () => { await Promise.resolve() })
  const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
  await act(async () => { ws.onopen?.() })
  return { rendu, ws }
}

HTMLElement.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  cleanup()
  localStorage.clear()
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

describe('Chat — bouton micro relogé dans l\'îlot du composer', () => {
  it('le micro est après "Adaptatif" quand la transcription est disponible', async () => {
    poserFetch(tableSaine())
    await rendreEtConnecter()
    const adaptatif = screen.getByText('Adaptatif')
    const micro = screen.getByTitle('Micro (maintenir)')
    expect(adaptatif.compareDocumentPosition(micro) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
