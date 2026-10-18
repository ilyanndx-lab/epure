import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'

import Chat from './Component'

/**
 * Reconnexion après démontage — LE bug corrigé ici (#70).
 *
 * `ws.onclose` programmait `setTimeout(connect, 2000)` sans jamais l'annuler.
 * Or le nettoyage de l'effet ferme le socket, ce qui déclenche `onclose` : un
 * chat démonté rouvrait donc `/ws/chat` deux secondes plus tard, puis à chaque
 * coupure, indéfiniment. Le même chemin se déclenchait sans démontage, à
 * chaque nouvelle exécution de l'effet (dépendance `onAssistantDone`, qui
 * change quand on bascule la lecture vocale ; double montage de StrictMode en
 * dev) : le socket de l'exécution précédente rouvrait une connexion qui
 * remplaçait `wsRef.current`, laissant l'autre ouverte.
 *
 * Contrairement aux autres fichiers de test du chat, le double de `WebSocket`
 * est ici FIDÈLE sur ce point : `close()` déclenche `onclose`, comme le
 * navigateur. Sans cela, le bug est invisible en test.
 */

function poserFetch() {
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
    '/chat/conversations': { conversations: [], total: 0 },
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
  fermé = false
  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this)
  }
  send() { /* sans objet ici */ }
  close() {
    if (this.fermé) return
    this.fermé = true
    this.onclose?.()
  }
}

async function rendreEtConnecter(props: Parameters<typeof Chat>[0] = {}) {
  poserFetch()
  vi.stubGlobal('WebSocket', FakeWebSocket)
  const rendu = render(<Chat {...props} />)
  await act(async () => { await Promise.resolve() })
  const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
  await act(async () => { ws.onopen?.() })
  // Minuteries fictives APRÈS le montage : seule la reconnexion nous intéresse,
  // pas les requêtes initiales.
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  return { rendu, ws }
}

// jsdom n'implémente pas `scrollIntoView` (même stub que les autres tests du chat)
HTMLElement.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  localStorage.clear()
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

describe('Chat — reconnexion du WebSocket', () => {
  it("démonté, le chat ne rouvre aucune WebSocket, même 2 s plus tard ou davantage", async () => {
    const { rendu, ws } = await rendreEtConnecter()
    expect(FakeWebSocket.instances).toHaveLength(1)

    rendu.unmount()
    expect(ws.fermé).toBe(true)
    await act(async () => { vi.advanceTimersByTime(10_000) })

    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('monté, une coupure du serveur est toujours suivie d’une reconnexion après 2 s', async () => {
    const { ws } = await rendreEtConnecter()

    await act(async () => { ws.onclose?.() })
    await act(async () => { vi.advanceTimersByTime(1_999) })
    expect(FakeWebSocket.instances).toHaveLength(1)
    await act(async () => { vi.advanceTimersByTime(1) })

    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it("une nouvelle exécution de l'effet ne laisse qu'une connexion : l'ancienne ne se reconnecte pas", async () => {
    const { rendu, ws } = await rendreEtConnecter({ onAssistantDone: () => {} })

    // onAssistantDone change (bascule de la lecture vocale) : l'effet se rejoue
    rendu.rerender(<Chat onAssistantDone={() => {}} />)
    expect(ws.fermé).toBe(true)
    expect(FakeWebSocket.instances).toHaveLength(2)
    await act(async () => { vi.advanceTimersByTime(10_000) })

    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(FakeWebSocket.instances[1].fermé).toBe(false)
  })
})
