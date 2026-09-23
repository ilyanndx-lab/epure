import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import RedemarrageRequis from './RedemarrageRequis'
import { useFluxEnCours } from '../redemarrage'

/**
 * Bandeau « Redémarrage requis » — ce qu'il affiche, et surtout ce qu'il ne
 * promet pas.
 *
 * Les modules ne se montent plus à chaud : l'écart chargé/voulu est calculé par
 * le backend (`GET /instance/redemarrage`), le bandeau l'affiche et demande le
 * redémarrage. Ce qui est éprouvé ici, dans l'ordre :
 *  - une réponse d'ERREUR (500, corps inattendu) n'affiche rien — ni bandeau
 *    fantôme ni plantage (même famille que `ModuleBar.test.tsx`) ;
 *  - avec tray : la demande part, et la page ne se recharge qu'une fois qu'un
 *    AUTRE processus répond (`boot_id` différent) ;
 *  - sans tray : le message « manuel » est affiché, rien n'est rechargé tant que
 *    le même processus répond ;
 *  - un flux en cours (génération) déclenche un avertissement, et un refus
 *    n'envoie rien.
 */

type Reponse = { status?: number; corps: unknown }

function poserFetch(table: Record<string, Reponse | (() => Reponse)>) {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String(input)
    const methode = init?.method ?? 'GET'
    const cle = Object.keys(table)
      .filter(k => { const [m, chemin] = k.split(' '); return m === methode && url.includes(chemin) })
      .sort((a, b) => b.length - a.length)[0]
    const entree = cle ? table[cle] : { status: 500, corps: { detail: 'boom' } }
    const { status = 200, corps } = typeof entree === 'function' ? entree() : entree
    return new Response(JSON.stringify(corps), { status, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', impl)
  return impl
}

const REQUIS = {
  requis: true, automatique: true, boot_id: 'b1',
  écarts: [{ id: 'hello', changement: 'à décharger' }, { id: 'zz', changement: 'à charger' }],
  échecs: {},
}

let reload: ReturnType<typeof vi.fn>

beforeEach(() => {
  localStorage.setItem('epure.apiToken', 'jeton-de-test')
  reload = vi.fn()
  Object.defineProperty(window, 'location', {
    value: { ...window.location, reload }, configurable: true,
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const posts = (f: ReturnType<typeof vi.fn>) =>
  f.mock.calls.filter(([u, i]) => String(u).includes('/instance/redemarrage') && (i as RequestInit | undefined)?.method === 'POST')

describe('RedemarrageRequis', () => {
  it("n'affiche rien quand aucun redémarrage n'est requis", async () => {
    const f = poserFetch({ 'GET /instance/redemarrage': { corps: { ...REQUIS, requis: false, écarts: [] } } })
    const { container } = render(<RedemarrageRequis />)
    await waitFor(() => expect(f).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it("une réponse d'erreur ou mal formée n'affiche rien et ne plante pas", async () => {
    for (const corps of [{ detail: 'boom' }, 'texte', null, { requis: 'oui', écarts: 'x' }]) {
      const f = poserFetch({ 'GET /instance/redemarrage': { status: corps === null ? 500 : 200, corps } })
      const { container, unmount } = render(<RedemarrageRequis />)
      await waitFor(() => expect(f).toHaveBeenCalled())
      expect(container.textContent).toBe('')
      unmount()
    }
  })

  it('avec tray : demande, attend un autre boot_id, puis recharge', async () => {
    const f = poserFetch({
      'GET /instance/redemarrage': { corps: REQUIS },
      'POST /instance/redemarrage': { corps: { déclenché: true, automatique: true, boot_id: 'b1' } },
      'GET /health': { corps: { ollama: true, model: 'm', boot_id: 'b2' } },
    })
    render(<RedemarrageRequis />)
    await waitFor(() => expect(screen.getByText('Redémarrage requis')).toBeTruthy())
    expect(screen.getByText(/hello — à retirer/)).toBeTruthy()
    expect(screen.getByText(/zz — à charger/)).toBeTruthy()

    fireEvent.click(screen.getByText('Redémarrer maintenant'))
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(posts(f)).toHaveLength(1)
  })

  it('sans tray : dit « manuel » et ne recharge pas tant que le même processus répond', async () => {
    poserFetch({
      'GET /instance/redemarrage': { corps: { ...REQUIS, automatique: false } },
      'POST /instance/redemarrage': {
        corps: { déclenché: false, automatique: false, boot_id: 'b1',
                 message: 'Redémarrage manuel requis : relancez le backend.' },
      },
      'GET /health': { corps: { ollama: true, model: 'm', boot_id: 'b1' } },
    })
    render(<RedemarrageRequis />)
    await waitFor(() => expect(screen.getByText('Redémarrer maintenant')).toBeTruthy())
    fireEvent.click(screen.getByText('Redémarrer maintenant'))
    await waitFor(() => expect(screen.getByText(/manuel requis/)).toBeTruthy())
    expect(reload).not.toHaveBeenCalled()
  })

  it('un POST refusé (401) est dit, sans prétendre redémarrer', async () => {
    poserFetch({
      'GET /instance/redemarrage': { corps: REQUIS },
      'POST /instance/redemarrage': { status: 401, corps: { detail: 'token' } },
      'GET /health': { corps: { boot_id: 'b1' } },
    })
    render(<RedemarrageRequis />)
    await waitFor(() => expect(screen.getByText('Redémarrer maintenant')).toBeTruthy())
    fireEvent.click(screen.getByText('Redémarrer maintenant'))
    await waitFor(() => expect(screen.getByText(/HTTP 401/)).toBeTruthy())
    expect(reload).not.toHaveBeenCalled()
  })

  it('prévient avant de couper un flux en cours, et un refus ne demande rien', async () => {
    const f = poserFetch({ 'GET /instance/redemarrage': { corps: REQUIS } })
    const confirmer = vi.spyOn(window, 'confirm').mockReturnValue(false)
    function Generation() { useFluxEnCours('une réponse du chat', true); return null }
    render(<><Generation /><RedemarrageRequis /></>)
    await waitFor(() => expect(screen.getByText('Redémarrer maintenant')).toBeTruthy())
    fireEvent.click(screen.getByText('Redémarrer maintenant'))
    expect(confirmer).toHaveBeenCalledTimes(1)
    expect(String(confirmer.mock.calls[0][0])).toContain('une réponse du chat')
    expect(posts(f)).toHaveLength(0)
  })
})
