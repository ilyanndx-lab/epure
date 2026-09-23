import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'

import Settings from './Component'
import { refreshInstance } from '../../instance'
import { fetchModules } from '../../modules'

/**
 * Réglages en onglets pilule — itération « passage à des onglets en pilule ».
 *
 * Aucune capture ne peut prouver deux des points de la spec (le
 * drag-and-drop de l'onglet Instance, et la disparition de l'onglet Atelier
 * quand ATELIER_PRESENT est faux) : ces tests les exercent réellement.
 *
 * Idiome repris de `ModuleBar.test.tsx` : une table URL → réponse, la clé la
 * plus longue qui matche, défaut 500 pour toute route oubliée (§8 de
 * CLAUDE.md — aucune réponse n'est crue sur sa forme).
 */

type Reponse = { status?: number; corps: unknown }

function poserFetch(table: Record<string, Reponse>) {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String(input)
    const cle = Object.keys(table)
      .filter(k => url.includes(k))
      .sort((a, b) => b.length - a.length)[0]
    const { status = 200, corps } = cle
      ? table[cle]
      : { corps: { detail: 'Erreur interne du serveur' }, status: 500 }
    void init
    return new Response(JSON.stringify(corps), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', impl)
  return impl
}

function tableSaine(): Record<string, Reponse> {
  return {
    '/pair': { corps: { token: 'jeton-de-test' } },
    '/memory/profile': { corps: {} },
    '/memory/sessions': { corps: { sessions: [] } },
    '/settings/api-keys': { corps: {} },
    '/settings/provider-models': { corps: { providers: {} } },
    '/context': { corps: {} },
    '/memory/consolidation-log': { corps: { log: [] } },
    '/models': { corps: {} },
    '/workshop/engines': {
      corps: {
        ollama: { disponible: true, raison: '' },
        aider: { disponible: false, raison: 'binaire absent' },
        claude_sub: { disponible: false, raison: 'non authentifié' },
        claude_gateway: { disponible: false, raison: 'non configurée' },
      },
    },
    '/quota/usage': { corps: {} },
    '/quota/deepseek-balance': { corps: { ok: false, raison: 'n/a' } },
    '/settings/catalogue': { corps: { modules: [] } },
    '/modules': { corps: { modules: [] } },
    '/instance/config': { corps: {} },
  }
}

/** Deux modules « catalogue » minimaux, pour le test de drag-and-drop. */
function manifeste(id: string, nom: string) {
  return {
    id, version: '1', nom, icon: 'Box', description: '',
    frontend: { component: '' }, backend: { prefix: '' },
    core_module: false, origin: 'catalogue', status: 'active', removable: true,
  }
}

async function rendre(ComposantSettings: typeof Settings = Settings) {
  // Recharge explicitement les deux stores singleton (instance.ts, modules.ts)
  // depuis la table posée par CE test : sans ça, un module_activés ou un
  // catalogue laissé par un test précédent fuiterait dans celui-ci.
  await act(async () => { await Promise.all([refreshInstance(), fetchModules()]) })
  const rendu = render(<ComposantSettings />)
  await act(async () => { await Promise.resolve() })
  return rendu
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllGlobals()
})

describe('Réglages — barre d’onglets en pilule', () => {
  it('affiche les 6 onglets (Atelier présent) et « Apparence » actif par défaut', async () => {
    poserFetch(tableSaine())
    await rendre()

    for (const label of ['Apparence', 'Instance', 'Catalogue', 'Atelier — moteurs', 'Profil', 'Avancé']) {
      expect(screen.getByRole('button', { name: label })).toBeTruthy()
    }
    // Contenu de l'onglet par défaut visible…
    expect(screen.getByText('Thème')).toBeTruthy()
    // …et aucun autre : la « Modules visibles » de l'onglet Instance ne doit
    // pas être montée en même temps que celui d'Apparence.
    expect(screen.queryByText('Modules visibles')).toBeNull()
  })

  it('cliquer un onglet affiche SON contenu et masque celui des autres', async () => {
    poserFetch(tableSaine())
    await rendre()

    await act(async () => { screen.getByRole('button', { name: 'Instance' }).click() })
    expect(screen.getByText('Modules visibles')).toBeTruthy()
    expect(screen.queryByText('Thème')).toBeNull()

    await act(async () => { screen.getByRole('button', { name: 'Atelier — moteurs' }).click() })
    // « Ollama (local) » apparaît deux fois sur cet onglet (le statut ET
    // l'option du sélecteur « Moteur par défaut ») : un texte propre à ce
    // seul onglet évite l'ambiguïté.
    expect(screen.getByText('Passerelle (claude_gateway)')).toBeTruthy()
    expect(screen.queryByText('Modules visibles')).toBeNull()

    await act(async () => { screen.getByRole('button', { name: 'Profil' }).click() })
    expect(screen.getByText("Style d'interaction")).toBeTruthy()
  })

  it('« Avancé » regroupe les 4 sections que le mockup ne montrait pas, toutes visibles ensemble', async () => {
    poserFetch(tableSaine())
    await rendre()

    await act(async () => { screen.getByRole('button', { name: 'Avancé' }).click() })
    expect(screen.getByText('Consolidation mémoire')).toBeTruthy()
    expect(screen.getByText('Quotas & Usage')).toBeTruthy()
    expect(screen.getByText('Clés API')).toBeTruthy()
    expect(screen.getByText('Sessions de révision')).toBeTruthy()
    // Rien d'un autre onglet ne doit s'être glissé dans le lot.
    expect(screen.queryByText('Thème')).toBeNull()
  })

  it("l'onglet actif est persisté (survit à un remontage du composant)", async () => {
    poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Profil' }).click() })
    expect(screen.getByText("Style d'interaction")).toBeTruthy()

    // usePersistentState écrit en localStorage avec un debounce de 400 ms
    // (cf. sa docstring) : on attend qu'il ait eu le temps de flusher plutôt
    // que de lire localStorage à chaud.
    await new Promise(r => setTimeout(r, 500))
    expect(JSON.parse(localStorage.getItem('epure.settings.activeTab') ?? '""')).toBe('profil')

    cleanup()
    await rendre()
    expect(screen.getByText("Style d'interaction")).toBeTruthy()
  })

  it("le drag-and-drop des modules de l'onglet Instance fonctionne toujours", async () => {
    // Deux modules actifs, dans l'ordre chat → docs : on glisse « docs » avant
    // « chat » et on vérifie que le PUT /instance/config envoyé porte le nouvel
    // ordre — pas une capture d'écran, qui ne peut pas prouver un geste.
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/modules': { corps: { modules: [manifeste('chat', 'Chat'), manifeste('docs', 'Docs')] } },
      '/instance/config': { corps: { modules_activés: ['chat', 'docs'] } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Instance' }).click() })

    const ligneChat = screen.getByText('Chat').closest('[draggable="true"]') as HTMLElement
    const ligneDocs = screen.getByText('Docs').closest('[draggable="true"]') as HTMLElement
    expect(ligneChat).toBeTruthy()
    expect(ligneDocs).toBeTruthy()

    await act(async () => {
      fireEvent.dragStart(ligneDocs)
      fireEvent.dragOver(ligneChat)
      fireEvent.drop(ligneChat)
    })

    const appelReorder = fetchMock.mock.calls.find(([input, init]) => {
      const url = typeof input === 'string' ? input : String(input)
      return url.includes('/instance/config') && (init as RequestInit | undefined)?.method === 'PUT'
    })
    expect(appelReorder).toBeTruthy()
    const corps = JSON.parse((appelReorder![1] as RequestInit).body as string)
    expect(corps.modules_activés).toEqual(['docs', 'chat'])
  })
})

describe('Réglages — Atelier absent du build (ATELIER_PRESENT=false)', () => {
  afterEach(() => { vi.doUnmock('../../atelier'); vi.resetModules() })

  it("l'onglet Atelier disparaît de la barre, et un onglet mémorisé retombe sur Apparence", async () => {
    // Onglet mémorisé d'un build QUI AVAIT l'Atelier : ne doit pas pointer
    // vers un onglet qui n'existe plus dans celui-ci (§ commentaire TABS de
    // settings/Component.tsx).
    localStorage.setItem('epure.settings.activeTab', JSON.stringify('atelier'))
    poserFetch(tableSaine())

    vi.resetModules()
    vi.doMock('../../atelier', () => ({ ATELIER_PRESENT: false }))
    const { default: SettingsSansAtelier } = await import('./Component')
    const { refreshInstance: refreshInstanceFrais } = await import('../../instance')
    const { fetchModules: fetchModulesFrais } = await import('../../modules')

    await act(async () => { await Promise.all([refreshInstanceFrais(), fetchModulesFrais()]) })
    render(<SettingsSansAtelier />)
    await act(async () => { await Promise.resolve() })

    expect(screen.queryByRole('button', { name: /Atelier/ })).toBeNull()
    // Repli sur Apparence plutôt qu'un onglet fantôme vide.
    expect(screen.getByText('Thème')).toBeTruthy()
  })
})

describe('Réglages — Tool calling', () => {
  it('affiche l’interrupteur général, la recherche et les 3 skills', async () => {
    poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    expect(screen.getByRole('switch', { name: 'Tool calling' })).toBeTruthy()
    expect(screen.getByPlaceholderText('Rechercher un outil…')).toBeTruthy()
    expect(screen.getByText('Recherche web')).toBeTruthy()
    expect(screen.getByText('Recherche dans l’historique')).toBeTruthy()
    expect(screen.getByText('Recherche approfondie')).toBeTruthy()
  })

  it('la recherche filtre la liste par nom', async () => {
    poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    fireEvent.change(screen.getByPlaceholderText('Rechercher un outil…'), {
      target: { value: 'historique' },
    })
    expect(screen.getByText('Recherche dans l’historique')).toBeTruthy()
    expect(screen.queryByText('Recherche web')).toBeNull()
    expect(screen.queryByText('Recherche approfondie')).toBeNull()
  })

  it("couper l'interrupteur général envoie tool_calling.enabled=false au backend", async () => {
    const fetchMock = poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })
    await act(async () => { screen.getByRole('switch', { name: 'Tool calling' }).click() })

    const appel = fetchMock.mock.calls.find(([input, init]) => {
      const url = typeof input === 'string' ? input : String(input)
      return url.includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    })
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.tool_calling.enabled).toBe(false)
    // L'objet ENTIER est renvoyé, pas un patch partiel (core/memory.py ne
    // fusionne pas en profondeur, cf. Component.tsx).
    expect(corps.tool_calling.skills.web_search).toBeTruthy()
  })

  it('un skill désactivé passe en fin de liste, grisé, avec le texte de réactivation', async () => {
    poserFetch({
      ...tableSaine(),
      '/context': { corps: { tool_calling: {
        enabled: true,
        skills: {
          web_search: { enabled: false },
          history_search: { enabled: true },
          recherche_approfondie: { enabled: true, budget: 4 },
        },
      } } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    expect(screen.getByText('Désactivé — cliquer pour réactiver.')).toBeTruthy()
  })

  it('recherche_approfondie désactivée par défaut : pas de curseur de budget', async () => {
    poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    expect(screen.queryByRole('slider')).toBeNull()
  })

  it('le curseur de budget est visible pour recherche_approfondie activée', async () => {
    poserFetch({
      ...tableSaine(),
      // Activée EXPLICITEMENT : elle est désactivée par défaut depuis le
      // 2026-09-23, et ces tests portent sur le curseur, pas sur le défaut.
      '/context': { corps: { tool_calling: {
        enabled: true,
        skills: {
          web_search: { enabled: true },
          history_search: { enabled: true },
          recherche_approfondie: { enabled: true, budget: 4 },
        },
      } } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    const curseur = screen.getByRole('slider') as HTMLInputElement
    expect(curseur.min).toBe('1')
    expect(curseur.max).toBe('10')
  })

  it('changer le curseur envoie le nouveau budget au backend', async () => {
    const fetchMock = poserFetch({
      ...tableSaine(),
      // Activée EXPLICITEMENT : elle est désactivée par défaut depuis le
      // 2026-09-23, et ces tests portent sur le curseur, pas sur le défaut.
      '/context': { corps: { tool_calling: {
        enabled: true,
        skills: {
          web_search: { enabled: true },
          history_search: { enabled: true },
          recherche_approfondie: { enabled: true, budget: 4 },
        },
      } } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    await act(async () => {
      fireEvent.change(screen.getByRole('slider'), { target: { value: '7' } })
    })

    const appel = fetchMock.mock.calls.find(([input, init]) => {
      const url = typeof input === 'string' ? input : String(input)
      return url.includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    })
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.tool_calling.skills.recherche_approfondie.budget).toBe(7)
  })

  // ── Skills personnalisés agentiques (Phase C) ─────────────────────────────
  //
  // Un objet `prefixes.personnalises[].agentique === true` est invocable par
  // le modèle : il doit apparaître ici, pas seulement dans « Préfixes &
  // commandes » (qui ne montre que les `prefixe_actif` vrais, cf. la Phase B
  // et son commentaire dans Component.tsx).

  function personnaliseAgentique(overrides: Partial<{
    id: string; nom: string; description: string; prefixe_actif: boolean; agentique: boolean; budget: number
  }> = {}) {
    return {
      id: 'skill-1',
      nom: 'Correcteur',
      trigger: null,
      description: 'Corrige les fautes de grammaire du message.',
      instruction: 'Corrige la grammaire.',
      prefixe_actif: false,
      agentique: true,
      budget: 4,
      ...overrides,
    }
  }

  it("un skill personnalisé agentique apparaît dans la grille, un NON agentique n'y apparaît pas", async () => {
    poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        personnaliseAgentique({ id: 'a', nom: 'Correcteur', agentique: true }),
        personnaliseAgentique({ id: 'b', nom: 'Reformule', agentique: false, prefixe_actif: true }),
      ] } } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    expect(screen.getByText('Correcteur')).toBeTruthy()
    expect(screen.queryByText('Reformule')).toBeNull()
  })

  it("l'état vide affiche un message plutôt qu'une grille amputée", async () => {
    poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    expect(screen.getByText(/Aucun skill personnalisé invocable par le modèle/)).toBeTruthy()
  })

  it('la recherche filtre aussi les skills personnalisés agentiques, par nom et description', async () => {
    poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        personnaliseAgentique({ id: 'a', nom: 'Correcteur' }),
      ] } } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    fireEvent.change(screen.getByPlaceholderText('Rechercher un outil…'), { target: { value: 'correcteur' } })
    expect(screen.getByText('Correcteur')).toBeTruthy()
    expect(screen.queryByText('Recherche web')).toBeNull()

    fireEvent.change(screen.getByPlaceholderText('Rechercher un outil…'), { target: { value: 'historique' } })
    expect(screen.queryByText('Correcteur')).toBeNull()
  })

  it('couper le toggle « Invocable par le modèle » envoie agentique=false sans toucher aux autres champs', async () => {
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        personnaliseAgentique({ id: 'a', nom: 'Correcteur', prefixe_actif: true, agentique: true }),
      ] } } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    await act(async () => {
      screen.getByRole('switch', { name: 'Invocable par le modèle — Correcteur' }).click()
    })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.prefixes.personnalises[0]).toMatchObject({
      id: 'a', nom: 'Correcteur', agentique: false, prefixe_actif: true,
    })
  })

  it("changer le curseur de budget d'un skill personnalisé envoie le bon PATCH", async () => {
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        personnaliseAgentique({ id: 'a', nom: 'Correcteur', budget: 4 }),
      ] } } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    await act(async () => {
      fireEvent.change(screen.getByRole('slider', { name: "Budget d'appels — Correcteur" }), { target: { value: '8' } })
    })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.prefixes.personnalises[0]).toMatchObject({ id: 'a', budget: 8 })
  })

  it("désactiver un skill PUREMENT agentique (sans préfixe) propose de le supprimer, plutôt que d'écrire un objet mort", async () => {
    // Même piège que celui déjà verrouillé côté « Préfixes & commandes », côté
    // symétrique : `prefixe_actif: false` + toggle « agentique » à faux
    // produirait un objet sans aucun moyen d'être déclenché.
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        personnaliseAgentique({ id: 'a', nom: 'Correcteur', prefixe_actif: false, agentique: true }),
      ] } } },
    })
    const confirmMock = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmMock)
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    await act(async () => {
      screen.getByRole('switch', { name: 'Invocable par le modèle — Correcteur' }).click()
    })

    expect(confirmMock).toHaveBeenCalled()
    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.prefixes.personnalises).toEqual([])
  })

  it("refuser la confirmation laisse le skill PUREMENT agentique inchangé", async () => {
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        personnaliseAgentique({ id: 'a', nom: 'Correcteur', prefixe_actif: false, agentique: true }),
      ] } } },
    })
    vi.stubGlobal('confirm', vi.fn(() => false))
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Tool calling' }).click() })

    await act(async () => {
      screen.getByRole('switch', { name: 'Invocable par le modèle — Correcteur' }).click()
    })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeUndefined()
    expect(screen.getByText('Correcteur')).toBeTruthy()
  })
})

describe('Réglages — Catalogue (grille de cartes)', () => {
  it('installer et supprimer un module du catalogue fonctionnent toujours après le passage en cartes', async () => {
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/settings/catalogue': { corps: { modules: [
        { id: 'kholle', nom: 'Kholle', description: 'Prépare des kholles.', icon: 'Box', installé: false },
        { id: 'docs', nom: 'Docs', description: 'Analyse de documents.', icon: 'Box', installé: true },
      ] } },
      '/settings/catalogue/kholle/install': { corps: {} },
      '/settings/modules/docs': { corps: {} },
    })
    vi.stubGlobal('confirm', vi.fn(() => true))
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Catalogue' }).click() })

    expect(screen.getByText('Kholle')).toBeTruthy()
    expect(screen.getByText('Docs')).toBeTruthy()

    await act(async () => { screen.getByRole('button', { name: /^Installer/ }).click() })
    const appelInstall = fetchMock.mock.calls.find(([input]) => String(input).includes('/settings/catalogue/kholle/install'))
    expect(appelInstall).toBeTruthy()

    await act(async () => { screen.getByRole('button', { name: /Supprimer/ }).click() })
    const appelSupprime = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/settings/modules/docs') && (init as RequestInit | undefined)?.method === 'DELETE'
    )
    expect(appelSupprime).toBeTruthy()
  })
})

describe('Réglages — Préfixes & commandes', () => {
  it('affiche les 5 commandes intégrées avec leur trigger actuel', async () => {
    poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    for (const trigger of ['@cours', '@strict', '@web', '@image', '@historique']) {
      expect(screen.getByText(trigger)).toBeTruthy()
    }
  })

  it('renommer une commande intégrée envoie le bon PATCH, et laisse les personnalisés intacts', async () => {
    // Un personnalisé déjà présent sur `/context` : le PATCH de renommage
    // (objet `prefixes` complet, pas de fusion profonde côté backend) doit le
    // reporter TEL QUEL, sans quoi renommer une commande intégrée effacerait
    // silencieusement les préfixes personnalisés de l'utilisateur.
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        { id: 'abc', nom: 'Reformule', trigger: '@reformule', description: 'Reformule le texte.', instruction: 'Reformule.', prefixe_actif: true, agentique: false, budget: 4 },
      ] } } },
    })
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    const carteWeb = screen.getByText('@web').closest('div')!.parentElement!.parentElement!
    await act(async () => {
      fireEvent.click(within(carteWeb).getByRole('button', { name: 'Renommer' }))
    })
    const champ = screen.getByPlaceholderText('@nouveautrigger')
    fireEvent.change(champ, { target: { value: '@internet' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Enregistrer' })) })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.prefixes.integres.web.trigger).toBe('@internet')
    expect(corps.prefixes.personnalises).toEqual([
      { id: 'abc', nom: 'Reformule', trigger: '@reformule', description: 'Reformule le texte.', instruction: 'Reformule.', prefixe_actif: true, agentique: false, budget: 4 },
    ])
  })

  it("désactiver un préfixe personnalisé NON agentique propose de le supprimer, plutôt que d'écrire un objet mort", async () => {
    // `agentique: false` + toggle « prefixe_actif » à faux produirait un objet
    // sans AUCUN moyen d'être déclenché — `normaliser_prefixes` (backend)
    // l'écarterait silencieusement au prochain PATCH (CLAUDE.md §8 : un refus
    // qui s'affiche comme un succès). Le composant doit demander confirmation
    // et supprimer plutôt que d'envoyer cet état.
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        { id: 'abc', nom: 'Reformule', trigger: '@reformule', description: 'Reformule le texte.', instruction: 'Reformule.', prefixe_actif: true, agentique: false, budget: 4 },
      ] } } },
    })
    const confirmMock = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmMock)
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    await act(async () => { screen.getByRole('switch', { name: 'Activer Reformule' }).click() })

    expect(confirmMock).toHaveBeenCalled()
    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    // Supprimé, pas désactivé avec les deux cases à faux.
    expect(corps.prefixes.personnalises).toEqual([])
  })

  it("refuser la confirmation laisse le préfixe personnalisé NON agentique inchangé", async () => {
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        { id: 'abc', nom: 'Reformule', trigger: '@reformule', description: 'Reformule le texte.', instruction: 'Reformule.', prefixe_actif: true, agentique: false, budget: 4 },
      ] } } },
    })
    vi.stubGlobal('confirm', vi.fn(() => false))
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    await act(async () => { screen.getByRole('switch', { name: 'Activer Reformule' }).click() })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeUndefined()
    expect(screen.getByText('Reformule')).toBeTruthy()
  })

  it('désactiver une commande intégrée envoie enabled=false', async () => {
    const fetchMock = poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    await act(async () => { screen.getByRole('switch', { name: 'Activer Strict' }).click() })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.prefixes.integres.strict.enabled).toBe(false)
    // Les 4 autres intégrées voyagent inchangées dans le même PATCH (objet
    // complet, pas de fusion profonde côté backend).
    expect(corps.prefixes.integres.cours.enabled).toBe(true)
  })

  it('créer un préfixe personnalisé sans aucune case cochée affiche un avertissement et n’enregistre rien', async () => {
    const fetchMock = poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    await act(async () => { screen.getByRole('button', { name: 'Créer un préfixe' }).click() })
    fireEvent.change(screen.getByPlaceholderText('Synthèse de cours'), { target: { value: 'Test' } })
    fireEvent.change(screen.getByPlaceholderText('Texte injecté dans le prompt au déclenchement…'), { target: { value: 'Instruction.' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Enregistrer' })) })

    expect(screen.getByText(/Cochez au moins/)).toBeTruthy()
    const appelPatch = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appelPatch).toBeUndefined()
  })

  it('créer un préfixe personnalisé déclenchable par un préfixe (une seule case) envoie l’objet complet', async () => {
    const fetchMock = poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    await act(async () => { screen.getByRole('button', { name: 'Créer un préfixe' }).click() })
    fireEvent.change(screen.getByPlaceholderText('Synthèse de cours'), { target: { value: 'Synthèse' } })
    fireEvent.change(screen.getByPlaceholderText('Texte injecté dans le prompt au déclenchement…'), { target: { value: 'Fais une synthèse.' } })
    await act(async () => {
      fireEvent.click(screen.getByText('Déclenchable par un préfixe'))
    })
    fireEvent.change(screen.getByPlaceholderText('@synthese'), { target: { value: '@synthese' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Enregistrer' })) })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.prefixes.personnalises).toHaveLength(1)
    expect(corps.prefixes.personnalises[0]).toMatchObject({
      nom: 'Synthèse',
      trigger: '@synthese',
      instruction: 'Fais une synthèse.',
      prefixe_actif: true,
      agentique: false,
    })
  })

  it('créer un préfixe personnalisé agentique (une seule case) exige une description et affiche le curseur de budget', async () => {
    const fetchMock = poserFetch(tableSaine())
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    await act(async () => { screen.getByRole('button', { name: 'Créer un préfixe' }).click() })
    fireEvent.change(screen.getByPlaceholderText('Synthèse de cours'), { target: { value: 'Correcteur' } })
    fireEvent.change(screen.getByPlaceholderText('Texte injecté dans le prompt au déclenchement…'), { target: { value: 'Corrige la grammaire.' } })
    await act(async () => {
      fireEvent.click(screen.getByText('Invocable par le modèle'))
    })

    // Sans description : refusé.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Enregistrer' })) })
    expect(screen.getByText(/description est requise/)).toBeTruthy()

    fireEvent.change(
      screen.getByPlaceholderText('Ce que ce préfixe fait — visible par le modèle s’il est invocable'),
      { target: { value: 'Corrige les fautes de grammaire du message.' } }
    )
    const curseur = screen.getByRole('slider') as HTMLInputElement
    expect(curseur.min).toBe('1')
    expect(curseur.max).toBe('10')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Enregistrer' })) })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.prefixes.personnalises[0]).toMatchObject({ agentique: true, prefixe_actif: false })
  })

  it('supprimer un préfixe personnalisé le retire de la liste envoyée', async () => {
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/context': { corps: { prefixes: { integres: {}, personnalises: [
        { id: 'abc', nom: 'Reformule', trigger: '@reformule', description: 'Reformule le texte.', instruction: 'Reformule.', prefixe_actif: true, agentique: false, budget: 4 },
      ] } } },
    })
    vi.stubGlobal('confirm', vi.fn(() => true))
    await rendre()
    await act(async () => { screen.getByRole('button', { name: 'Préfixes & commandes' }).click() })

    expect(screen.getByText('Reformule')).toBeTruthy()
    await act(async () => { screen.getByRole('button', { name: 'Supprimer' }).click() })

    const appel = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/context/settings') && (init as RequestInit | undefined)?.method === 'PATCH'
    )
    expect(appel).toBeTruthy()
    const corps = JSON.parse((appel![1] as RequestInit).body as string)
    expect(corps.prefixes.personnalises).toEqual([])
  })
})
