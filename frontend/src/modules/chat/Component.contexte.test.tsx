import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'

import Chat from './Component'
import CamembertContexte from './CamembertContexte'

/**
 * Contexte restant : une jauge qui ne doit JAMAIS inventer son dénominateur.
 *
 * Toute la valeur de cet indicateur tient à une propriété, et c'est celle que ce
 * fichier verrouille : **il disparaît plutôt que d'afficher un chiffre faux**.
 * Un pourcentage de contexte est lu à l'instant précis où l'on décide si l'on
 * peut encore poser sa question ; un rapport plausible mais faux (« 100 %
 * restants » sur une fenêtre inconnue, un pourcentage calculé sur la fenêtre
 * d'un AUTRE modèle) est plus nuisible que pas d'indicateur du tout. C'est la
 * règle posée côté backend (`core/fenetre_contexte.py`, qui rend `None` au lieu
 * d'un `n_ctx` par défaut), et elle se rejoue ici au niveau du rendu.
 *
 * Deux niveaux, volontairement :
 *
 * 1. **Le composant seul** — les règles d'affichage (seuils, bornage, absence)
 *    s'éprouvent sans WebSocket ni backend. C'est là qu'elles vivent.
 * 2. **Le chat entier** — que la valeur arrive bien jusqu'à l'en-tête, par les
 *    deux chemins qui doivent montrer la même chose : la trame `stats` pendant
 *    la génération, et l'historique après un F5.
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
 * Table du chat, réduite au strict nécessaire.
 *
 * **La règle est une seule : la route la plus SPÉCIFIQUE se déclare en
 * premier.** `poserFetch` retient la première clé que l'URL contient, et trois
 * recouvrements se croisent ici :
 *
 * - `contexte` CONTIENT `context` → `/models/contexte` déclarée après
 *   `/context` ne serait jamais atteinte, `/context` répondrait à sa place ;
 * - `/chat/conversations/conv-a` contient `/chat/conversations` → déclarée
 *   après, l'historique chargé serait la LISTE des conversations (donc aucun
 *   message), et le test accuserait la jauge au lieu du bouchon ;
 * - `/models/contexte` contient `/models` — même famille.
 *
 * Conséquence assumée : l'ordre est fixé ICI, dans une seule fonction, plutôt
 * que laissé à chaque appelant qui étalerait ses routes après coup. C'est
 * exactement le piège que `Component.conversation.test.tsx` documente pour
 * `/models/lmstudio/chargement`, et la raison pour laquelle son `tableSaine`
 * insère ses routes spécifiques AVANT les génériques.
 */
function table(
  fenetre: { fenetre: number | null; source: string | null },
  conversation: unknown = { messages: [] },
) {
  return {
    '/pair': { corps: { token: 'jeton-de-test' } },
    // ── Spécifiques d'abord ──────────────────────────────────────────────
    '/models/contexte': { corps: { modele: 'qwen2.5:7b', ...fenetre } },
    '/chat/conversations/conv-a': { corps: conversation },
    // ── Génériques ensuite ───────────────────────────────────────────────
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
    '/chat/conversations': { corps: { conversations: [], total: 0 } },
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

function envoyer(ws: FakeWebSocket, data: Record<string, unknown>) {
  ws.onmessage?.({ data: JSON.stringify(data) })
}

/**
 * Laisse les chaînes de promesses (fetch → .json() → setState) se dérouler.
 *
 * Plusieurs tours, et pas un seul : au montage s'enchaînent la lecture de
 * `/context` (qui fixe le modèle actif), celle de `/models/contexte` (qui en
 * DÉPEND — elle ne part qu'une fois `modeleActifId` connu) et celle de
 * l'historique de la conversation affichée. Un unique tour de boucle
 * d'événements laisse la troisième en vol, et le test échouerait en accusant à
 * tort la règle d'affichage.
 */
async function attendre(tours = 4) {
  for (let i = 0; i < tours; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

async function rendreEtConnecter() {
  const rendu = render(<Chat />)
  await act(async () => { await Promise.resolve() })
  const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
  await act(async () => { ws.onopen?.() })
  return { rendu, ws }
}

/** Le nœud accessible de la jauge — `null` quand elle ne s'affiche pas. */
const jauge = () => screen.queryByRole('img', { name: /Contexte restant/ })

HTMLElement.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  cleanup()
  localStorage.clear()
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

// ─────────────────────────────────────────────────────────────────────────────
// 1. Le composant seul
// ─────────────────────────────────────────────────────────────────────────────

describe('CamembertContexte — ce qui s\'affiche, et ce qui ne s\'affiche pas', () => {
  const rendre = (fenetre: number | null, utilise: number | null, source?: string | null) =>
    render(<CamembertContexte fenetre={fenetre} utilise={utilise} source={source ?? null} />)

  it('sans fenêtre connue : rien dans le DOM', () => {
    // Le cœur de la règle. Pas d'anneau vide, pas de « ? % », pas de « 100 % »
    // de consolation : rien. C'est le cas d'un fournisseur muet (Cerebras,
    // NVIDIA, DeepSeek, Groq — mesurés le 2026-09-20) et d'un Ollama dont le
    // modèle n'est pas encore chargé (`/api/ps` ne liste que les résidents).
    rendre(null, 8192, null)
    expect(jauge()).toBeNull()
  })

  it('fenêtre nulle ou négative : rien non plus, sans diviser par zéro', () => {
    rendre(0, 100, 'x')
    expect(jauge()).toBeNull()
    cleanup()
    rendre(-1, 100, 'x')
    expect(jauge()).toBeNull()
  })

  it('aucun tour mesuré : rien, même avec une fenêtre connue', () => {
    // Fil neuf : la fenêtre est lue, mais rien n'a encore été consommé. Un
    // « 100 % » serait un chiffre inventé, pas une mesure.
    rendre(32768, null, 'ollama /api/ps')
    expect(jauge()).toBeNull()
  })

  it('au-dessus de 50 % restants : le pourcentage, l\'arc, et la teinte « success »', () => {
    rendre(1000, 250, 'ollama /api/ps') // restant 750 → 75 %
    expect(screen.getByText('75 %')).toBeTruthy()
    const arc = document.querySelector('[stroke-dasharray]')
    expect(arc?.getAttribute('stroke-dasharray')).toBe('75 25')
    expect(arc?.getAttribute('class')).toContain('stroke-success')
  })

  it('de 20 à 50 % : teinte « warning »', () => {
    rendre(1000, 700, 'x') // restant 300 → 30 %
    expect(screen.getByText('30 %')).toBeTruthy()
    expect(document.querySelector('[stroke-dasharray]')?.getAttribute('class')).toContain('stroke-warning')
  })

  it('sous 20 % : teinte « error », et la sévérité se lit aussi sur le fond de la jauge', () => {
    rendre(1000, 850, 'x') // restant 150 → 15 %
    expect(screen.getByText('15 %')).toBeTruthy()
    const cercles = document.querySelectorAll('circle')
    // Le fond de la jauge porte la MÊME teinte, éteinte — pas un gris neutre :
    // la sévérité doit se lire sur tout l'anneau, pas seulement sur l'arc.
    expect(cercles[0].getAttribute('class')).toContain('stroke-error')
    expect(cercles[0].getAttribute('class')).toContain('opacity-20')
  })

  it('le nombre ne prend jamais la couleur de la sévérité', () => {
    // Le texte porte les jetons de texte. Colorer les chiffres ferait porter
    // l'information par la couleur SEULE — et l'écart ambre↔vert mesuré à
    // ΔE 7,3 en protanopie n'est pas fiable. La couleur double, elle ne porte pas.
    rendre(1000, 850, 'x')
    const nombre = screen.getByText('15 %')
    expect(nombre.getAttribute('class')).toContain('text-secondary')
    expect(nombre.getAttribute('class')).not.toContain('stroke-')
  })

  it('un contexte plus gros que la fenêtre est borné, pas affiché en négatif', () => {
    // Cas réel : `num_ctx` change en cours de session (modèle rechargé), ou le
    // fournisseur annonce plus que la fenêtre qu'on a lue. Sans bornage, le
    // restant serait négatif et le `strokeDasharray` absurde.
    rendre(1000, 1500, 'x')
    expect(screen.getByText('0 %')).toBeTruthy()
    expect(document.querySelector('[stroke-dasharray]')).toBeNull()
  })

  it('à 100 % restants, l\'anneau est plein sans motif de tirets', () => {
    // `strokeDasharray="100 0"` est un motif dégénéré : selon le moteur de
    // rendu, il peut ne RIEN dessiner — soit l'inverse exact de la vérité au
    // moment précis où l'on croit la fenêtre vide. D'où la branche dédiée.
    rendre(1000, 0, 'x')
    expect(screen.getByText('100 %')).toBeTruthy()
    expect(document.querySelector('[stroke-dasharray]')).toBeNull()
  })

  it('l\'infobulle nomme l\'ORIGINE du chiffre', () => {
    // Un nombre sans provenance n'est pas vérifiable. C'est la raison d'être du
    // champ `source` remonté par le backend, et le motif de la jauge native
    // (le composant `Tooltip` partagé est en `whitespace-nowrap` : collé au bord
    // droit de l'en-tête, il sortirait de l'écran).
    rendre(32768, 12288, 'ollama /api/ps')
    expect(screen.getByTitle(/fenêtre lue sur ollama \/api\/ps/)).toBeTruthy()
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// 2. Le chat entier — les deux chemins doivent montrer la même chose
// ─────────────────────────────────────────────────────────────────────────────

describe('Chat — contexte restant en direct et après rechargement', () => {
  it('la trame `stats` puis `done` posent le contexte du tour, et la jauge apparaît', async () => {
    localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
    poserFetch(table({ fenetre: 32768, source: 'ollama /api/ps' }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()
    await attendre()

    // Rien tant qu'aucun tour n'a été mesuré — même si la fenêtre est connue.
    expect(jauge()).toBeNull()

    await act(async () => { envoyer(ws, { type: 'token', content: 'Bonjour', conversation_id: 'conv-a' }) })
    await act(async () => {
      envoyer(ws, {
        type: 'stats', conversation_id: 'conv-a',
        prompt_tokens: 4096, output_tokens: 12, eval_duration_ms: 900,
        contexte_tokens: 8192,
      })
    })
    await act(async () => {
      envoyer(ws, { type: 'done', conversation_id: 'conv-a', 'modèle': 'qwen2.5:7b', horodatage: '2026-09-20T10:00:00' })
    })
    await attendre()

    // restant = 32 768 − 8 192 = 24 576 → 75 %.
    //
    // Le numérateur est `contexte_tokens` (le DERNIER round), PAS
    // `prompt_tokens`. Les deux sortent de la même trame et ne mesurent pas la
    // même chose : `prompt_tokens` additionne tous les rounds d'appel d'outil —
    // c'est ce qui se facture. Prendre celui-là donnerait 87 %, un chiffre faux
    // et crédible.
    expect(screen.getByText('75 %')).toBeTruthy()
  })

  it('modèle à froid au montage : la fenêtre est RELUE quand le premier tour mesuré arrive', async () => {
    // Le scénario réel d'un modèle Ollama, et le seul qui compte : au montage,
    // rien n'est chargé — `/api/ps` ne liste que les RÉSIDENTS — donc la fenêtre
    // est inconnue. Or c'est le premier message envoyé qui fait charger le
    // modèle. La fenêtre devient donc connaissable à l'instant précis où le
    // numérateur arrive, et à aucun autre.
    //
    // Ne relire qu'au changement de modèle laissait la jauge invisible pour tout
    // le premier tour d'une session — puis, comme rien d'autre ne changeait la
    // dépendance, pour tous les suivants. Elle n'apparaissait qu'après un aller-
    // retour de modèle ou un F5 : c'est-à-dire en perdant la conversation qui la
    // justifiait.
    localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
    const routes = table({ fenetre: null, source: null })
    poserFetch(routes)
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()
    await attendre()
    expect(jauge()).toBeNull()

    // Le tour a fait charger `qwen2.5:7b` : `/api/ps` le liste à présent, et
    // l'endpoint répond pour de bon. Le bouchon répond par mutation, donc la
    // relecture voit la NOUVELLE valeur et non celle du montage.
    routes['/models/contexte'] = {
      corps: { modele: 'qwen2.5:7b', fenetre: 32768, source: 'ollama /api/ps' },
    }

    await act(async () => { envoyer(ws, { type: 'token', content: 'Bonjour', conversation_id: 'conv-a' }) })
    await act(async () => {
      envoyer(ws, {
        type: 'stats', conversation_id: 'conv-a',
        prompt_tokens: 4096, output_tokens: 12, eval_duration_ms: 900,
        contexte_tokens: 8192,
      })
    })
    await act(async () => {
      envoyer(ws, { type: 'done', conversation_id: 'conv-a', 'modèle': 'qwen2.5:7b', horodatage: '2026-09-20T10:00:00' })
    })
    await attendre()

    // restant = 32 768 − 8 192 = 24 576 → 75 %.
    expect(screen.getByText('75 %')).toBeTruthy()
  })

  it('fournisseur muet : la trame `stats` sans `contexte_tokens` ne fait rien apparaître', async () => {
    localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
    poserFetch(table({ fenetre: null, source: null }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const { ws } = await rendreEtConnecter()
    await attendre()

    await act(async () => { envoyer(ws, { type: 'token', content: 'Bonjour', conversation_id: 'conv-a' }) })
    await act(async () => {
      envoyer(ws, {
        type: 'stats', conversation_id: 'conv-a',
        prompt_tokens: 4096, output_tokens: 12, eval_duration_ms: 900,
      })
    })
    await act(async () => {
      envoyer(ws, { type: 'done', conversation_id: 'conv-a', 'modèle': 'qwen2.5:7b', horodatage: '2026-09-20T10:00:00' })
    })
    await attendre()

    expect(jauge()).toBeNull()
  })

  it('après rechargement : le contexte du tour est restauré depuis l\'historique', async () => {
    // Ce que le disque porte après le tour de `router.py` : la métadonnée
    // `contexte_tokens` à côté de `sources` / `trace_recherche`. C'est elle qui
    // fait survivre l'indicateur à un F5 — sans elle, l'information n'existerait
    // que dans la trame WebSocket du tour, donc manquerait précisément sur les
    // conversations assez longues pour que la question se pose.
    localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
    poserFetch(table({ fenetre: 32768, source: 'ollama /api/ps' }, {
      messages: [
        { role: 'user', content: 'question' },
        { role: 'assistant', content: 'reponse', 'modèle': 'qwen2.5:7b', contexte_tokens: 24576 },
      ],
    }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    await rendreEtConnecter()
    await attendre()

    // restant = 32 768 − 24 576 = 8 192 → 25 %.
    expect(screen.getByText('25 %')).toBeTruthy()
  })

  it('le dernier tour vient d\'un AUTRE modèle que l\'actif : pas de jauge', async () => {
    // Le garde-fou qui empêche le seul chiffre faux qui serait vraiment
    // trompeur : la fenêtre lue décrit le modèle SÉLECTIONNÉ, le numérateur
    // vient du modèle qui a RÉPONDU. Après un changement de modèle en cours de
    // fil, les deux ne mesurent plus la même chose. On n'affiche rien plutôt
    // qu'un rapport croisé qui aurait l'air parfaitement légitime.
    localStorage.setItem('epure.chat.conversationId', JSON.stringify('conv-a'))
    poserFetch(table({ fenetre: 32768, source: 'ollama /api/ps' }, {
      messages: [
        { role: 'user', content: 'question' },
        { role: 'assistant', content: 'reponse', 'modèle': 'gemini:gemini-2.5-flash', contexte_tokens: 24576 },
      ],
    }))
    vi.stubGlobal('WebSocket', FakeWebSocket)
    await rendreEtConnecter()
    await attendre()

    expect(jauge()).toBeNull()
  })
})
