import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import Chat from './Component'

/**
 * Popover « Paramètres de la conversation » — fusion du header (effort +
 * préfixes @ + commandes /) et de l'ancien panel « skills » de `ModuleBar`
 * (3 toggles + 2 consignes), itération 2 du header de conversation (défaut 3).
 *
 * Les quatre premiers cas (« ModuleBar — toggle de réflexion ») sont PORTÉS
 * tels quels depuis `components/ModuleBar.test.tsx` : ce toggle a migré avec
 * tout le panel dans ce popover, et `ModuleBar.tsx` ne le rend plus du tout
 * (`showSkills` n'avait que Chat comme consommateur — code supprimé, pas
 * laissé mort). Le JSX porté est identique à l'octet, donc les mêmes
 * sélecteurs (`getByRole('switch', {name})`, `getByText(...)`) tiennent.
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

const CONTEXTE_OK = {
  'modèle_actif': 'qwen2.5:7b',
  strict_mode: false,
  'instruction_générale': '',
}

const MODELES_OLLAMA = {
  local: [{ id: 'qwen2.5:7b', nom: 'Qwen2.5 7B', provider: 'ollama', disponible: true }],
  local_npu: [], cloud: {}, fournisseurs: {}, recommandations: {},
}
const MODELES_GEMINI = {
  local: [], local_npu: [],
  cloud: {
    rapide: [{ id: 'gemini:gemini-2.5-flash', nom: 'Gemini 2.5 Flash', provider: 'gemini', disponible: true }],
    puissant: [], long_contexte: [],
  },
  fournisseurs: {}, recommandations: {},
}
const MODELES_GROQ = {
  local: [], local_npu: [],
  cloud: {
    rapide: [{ id: 'groq:openai/gpt-oss-20b', nom: 'GPT OSS 20B', provider: 'groq', disponible: true }],
    puissant: [], long_contexte: [],
  },
  fournisseurs: {}, recommandations: {},
}
// LM Studio n'est pas un fournisseur CLOUD (serveur local, cf.
// core/instance.py:_FOURNISSEURS_CLOUD) mais rejoint quand même la branche
// « budget de réflexion seul » du toggle : LM Studio n'expose aucun paramètre
// stable pour couper sa réflexion (core/llm.py::_stream_openai). D'où
// `local_lmstudio`, pas `cloud`, pour porter le modèle actif de ce test.
const MODELES_LMSTUDIO = {
  local: [], local_npu: [],
  local_lmstudio: [{
    id: 'lmstudio:llama-3.1-8b-instruct', nom: 'llama-3.1-8b-instruct',
    provider: 'lmstudio', disponible: true,
  }],
  cloud: {}, fournisseurs: {}, recommandations: {},
}

function tableSaine(extra: Record<string, { status?: number; corps: unknown }> = {}) {
  return {
    '/pair': { corps: { token: 'jeton-de-test' } },
    '/context': { corps: CONTEXTE_OK },
    '/rag/files': { corps: { files: [] } },
    '/rag/capabilities': { corps: { 'état': 'prêt', disponible: true, message: '', cause: '', 'taille_estimée_mo': 0 } },
    '/models': { corps: MODELES_OLLAMA },
    '/voice/capabilities': {
      corps: {
        transcription: { disponible: false, manquants: [], raison: '' },
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

async function ouvrirParametres(table: Record<string, { status?: number; corps: unknown }>) {
  poserFetch(table)
  const rendu = await rendreEtConnecter()
  await ouvrir('Paramètres de la conversation')
  return rendu
}

describe('Chat — toggle de réflexion, honnête selon le provider (popover Paramètres)', () => {
  it("masque le toggle et remplace par un texte explicite quand gemini est actif — aucun effet à annoncer", async () => {
    await ouvrirParametres(tableSaine({
      '/context': { corps: { ...CONTEXTE_OK, 'modèle_actif': 'gemini:gemini-2.5-flash' } },
      '/models': { corps: MODELES_GEMINI },
    }))
    await waitFor(() => expect(screen.getByText(/non disponible sur Gemini/)).toBeTruthy())
    // Aucun contrôle qui accepterait une valeur sans effet, sous aucun libellé.
    expect(screen.queryByRole('switch', { name: 'Réflexion du modèle' })).toBeNull()
    expect(screen.queryByRole('switch', { name: 'Budget de réflexion (tokens)' })).toBeNull()
  })

  it("relabellise en « budget de réflexion » sur un fournisseur cloud qui ne montre jamais sa pensée (groq)", async () => {
    await ouvrirParametres(tableSaine({
      '/context': { corps: { ...CONTEXTE_OK, 'modèle_actif': 'groq:openai/gpt-oss-20b' } },
      '/models': { corps: MODELES_GROQ },
    }))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Budget de réflexion (tokens)' })).toBeTruthy())
    expect(screen.getByText(/relève seulement le plafond de tokens/)).toBeTruthy()
    expect(screen.queryByText(/Sa réflexion s'affiche pendant l'attente/)).toBeNull()
  })

  it('garde le libellé et le texte actuels sur ollama — non-régression', async () => {
    await ouvrirParametres(tableSaine())
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Réflexion du modèle' })).toBeTruthy())
    expect(screen.getByText(/Sa réflexion s'affiche pendant l'attente/)).toBeTruthy()
  })

  it("relabellise en « budget de réflexion » sur LM Studio — aucun paramètre stable côté serveur", async () => {
    await ouvrirParametres(tableSaine({
      '/context': { corps: { ...CONTEXTE_OK, 'modèle_actif': 'lmstudio:llama-3.1-8b-instruct' } },
      '/models': { corps: MODELES_LMSTUDIO },
    }))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Budget de réflexion (tokens)' })).toBeTruthy())
    expect(screen.getByText(/relève seulement le plafond de tokens/)).toBeTruthy()
    expect(screen.queryByText(/Sa réflexion s'affiche pendant l'attente/)).toBeNull()
  })
})

describe('Chat — popover Paramètres fusionné, aucun doublon avec ModuleBar', () => {
  it('un seul popover réunit les 3 toggles, préfixes/commandes et les 2 consignes (effort a déménagé dans le composer, itération 3)', async () => {
    await ouvrirParametres(tableSaine())
    expect(screen.getByRole('switch', { name: 'Mode strict' })).toBeTruthy()
    expect(screen.getByRole('switch', { name: 'Réflexion du modèle' })).toBeTruthy()
    expect(screen.getByText('Préfixes @')).toBeTruthy()
    expect(screen.getByText('Commandes /')).toBeTruthy()
    expect(screen.getByText('Consigne générale')).toBeTruthy()
    expect(screen.getByText('Consigne de cette conversation')).toBeTruthy()
  })

  it("ModuleBar ne propose plus aucun bouton « Paramètres de session » ni « Modèle » pour Chat", async () => {
    poserFetch(tableSaine())
    await rendreEtConnecter()
    expect(screen.queryByTitle('Paramètres de session')).toBeNull()
    expect(screen.queryByTitle('Modèle')).toBeNull()
  })
})

/**
 * Chip modèle du header — itération 3 : ce n'est plus une liste à plat mais
 * le panneau modèle COMPLET de `ModuleBar` (recommandations, verdicts
 * matériel, contrôles FLM, mémoire Ollama...), porté par un portail React
 * jusqu'à l'ancre du header (`modelPanelPortalTarget`) plutôt que dupliqué.
 * `qwen2.5:7b` n'est pas dans les recommandations curées de `chat`
 * (`MODULE_RECOMMENDATIONS` dans `ModuleBar.tsx`), donc il faut d'abord
 * ouvrir "Voir tous les modèles" pour l'atteindre — exactement le
 * comportement qu'on avait avant que le chip existe.
 */
describe('Chat — chip modèle du header (panneau complet de ModuleBar, porté par portail)', () => {
  it('ouvre le panneau modèle complet, et changer de modèle appelle bien PATCH /context/settings', async () => {
    const fetchMock = poserFetch(tableSaine())
    await rendreEtConnecter()
    await ouvrir('Changer de modèle')
    // Panneau recommandé par défaut — qwen2.5:7b n'y figure pas.
    expect(screen.getByText('Voir tous les modèles')).toBeTruthy()
    expect(screen.queryByText('Qwen2.5 7B')).toBeNull()

    await act(async () => { fireEvent.click(screen.getByText('Voir tous les modèles')) })
    await waitFor(() => expect(screen.getByText('Qwen2.5 7B')).toBeTruthy())

    await act(async () => { fireEvent.click(screen.getByText('Qwen2.5 7B')) })

    const appelPatch = fetchMock.mock.calls.find(([input, init]) => {
      const url = typeof input === 'string' ? input : String(input)
      return url.includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    })
    expect(appelPatch).toBeTruthy()
    const corps = JSON.parse((appelPatch![1] as RequestInit).body as string)
    expect(corps['modèle_actif']).toBe('qwen2.5:7b')
  })

})

describe('Chat — popover Paramètres, préfixes filtrés sur les actifs (Réglages › Préfixes & commandes)', () => {
  it('masque un préfixe intégré désactivé et affiche un préfixe personnalisé actif', async () => {
    await ouvrirParametres(tableSaine({
      '/context': {
        corps: {
          ...CONTEXTE_OK,
          prefixes: {
            integres: {
              cours: { trigger: '@cours', enabled: false },
              strict: { trigger: '@strict', enabled: true },
              web: { trigger: '@web', enabled: true },
              image: { trigger: '@image', enabled: true },
              historique: { trigger: '@historique', enabled: true },
            },
            personnalises: [{
              id: 'p1', nom: 'Synthèse', trigger: '@synthese', description: 'Résumé structuré',
              prefixe_actif: true, agentique: false, budget: 4,
            }],
          },
        },
      },
    }))
    await waitFor(() => expect(screen.getByText('@synthese')).toBeTruthy())
    expect(screen.queryByText('@cours')).toBeNull()
    expect(screen.getByText('@strict')).toBeTruthy()
    // `@mémoire` n'a pas d'équivalent backend désactivable — toujours affiché.
    expect(screen.getByText('@mémoire')).toBeTruthy()
    expect(screen.getByText('Résumé structuré')).toBeTruthy()
  })
})

describe('Chat — niveaux d\'effort dans l\'îlot du composer', () => {
  it("les 5 pilules d'effort sont dans le composer, plus dans le popover Paramètres", async () => {
    poserFetch(tableSaine())
    await rendreEtConnecter()
    for (const label of ['Direct', 'Low', 'Medium', 'High', 'Adaptatif']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    await ouvrir('Paramètres de la conversation')
    expect(screen.queryByText('Effort')).toBeNull()
  })

  it('le bouton "Relancer le dernier message" a disparu du composer', async () => {
    poserFetch(tableSaine())
    await rendreEtConnecter()
    expect(screen.queryByTitle('Relancer le dernier message')).toBeNull()
  })
})

/**
 * Bouton "Fichiers" — itération 4 : relogé dans l'îlot du composer (à
 * gauche de "Direct"), porté par portail depuis `ModuleBar` comme le chip
 * modèle. Le test du micro (même itération) vit dans son propre fichier —
 * cf. `Component.micComposer.test.tsx` — parce que `voix.ts` mémorise son
 * verdict au niveau du MODULE (pas du composant) pour toute la durée du
 * fichier de test, une fois `chargerVoix()` déclenché par un premier
 * montage : un second test dans CE fichier, avec un fixture différent
 * pour `/voice/capabilities`, ne le verrait jamais appliqué.
 */
describe('Chat — bouton "Fichiers" relogé dans l\'îlot du composer', () => {
  it('le bouton "Fichiers" est avant "Direct", ouvre le vrai panneau de ModuleBar', async () => {
    poserFetch(tableSaine())
    await rendreEtConnecter()
    const fichiers = screen.getByTitle('Fichiers')
    const direct = screen.getByText('Direct')
    expect(fichiers.compareDocumentPosition(direct) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    await ouvrir('Fichiers')
    expect(screen.getByText('Glisser un fichier ici · Cliquer pour parcourir')).toBeTruthy()
  })
})
