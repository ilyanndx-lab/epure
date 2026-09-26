import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import JournalCsp from './JournalCsp'

/**
 * Journal de la CSP en observation (Réglages) — ce qu'il affiche, et la forme
 * d'un backend qui refuse : un 401/500 donne un message, jamais un tableau
 * fantôme ni un plantage (même famille que `RedemarrageRequis.test.tsx`).
 * Le contenu vient d'un point de collecte sans token : il doit sortir en TEXTE.
 */

type Reponse = { status?: number; corps: unknown }

/** Routage par « MÉTHODE chemin » ; une liste est servie dans l'ordre (le
 *  dernier élément se répète). `/pair` rend un token : `api.ts` lit le sien au
 *  chargement du module, avant `beforeEach`, et appaire au premier appel. */
function poserFetch(table: Record<string, Reponse | Reponse[]>) {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String(input)
    const methode = init?.method ?? 'GET'
    if (url.endsWith('/pair')) {
      return new Response(JSON.stringify({ token: 'jeton-de-test' }), { status: 200 })
    }
    const entree = table[`${methode} ${new URL(url).pathname}`] ?? { status: 500, corps: { detail: 'boom' } }
    const { status = 200, corps } = Array.isArray(entree)
      ? (entree.length > 1 ? entree.shift()! : entree[0]) : entree
    return new Response(JSON.stringify(corps), { status, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', impl)
  return impl
}

const ENTREE = {
  directive: 'connect-src', bloqué: 'https://exfil.example', document: 'http://localhost:5173',
  nombre: 3, dernier: 1_790_000_000,
}

beforeEach(() => { localStorage.setItem('epure.apiToken', 'jeton-de-test') })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear() })

describe('JournalCsp', () => {
  it('affiche les entrées agrégées', async () => {
    poserFetch({ 'GET /instance/csp': { corps: { mode: 'report-only', entrées: [ENTREE] } } })
    render(<JournalCsp />)
    expect(await screen.findByText('https://exfil.example')).toBeTruthy()
    expect(screen.getByText('connect-src')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
  })

  it('journal vide : le dit, sans tableau', async () => {
    poserFetch({ 'GET /instance/csp': { corps: { entrées: [] } } })
    render(<JournalCsp />)
    expect(await screen.findByText('Aucune violation signalée.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('backend qui refuse : message, pas de tableau', async () => {
    poserFetch({ 'GET /instance/csp': { status: 500, corps: { detail: 'boom' } } })
    render(<JournalCsp />)
    expect(await screen.findByText(/HTTP 500/)).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('contenu hostile rendu en texte, jamais en HTML', async () => {
    poserFetch({ 'GET /instance/csp': { corps: { entrées: [{ ...ENTREE, bloqué: '<img src=x onerror=alert(1)>' }] } } })
    const { container } = render(<JournalCsp />)
    expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })

  it('vider rappelle le backend puis recharge', async () => {
    const f = poserFetch({
      'GET /instance/csp': [{ corps: { entrées: [ENTREE] } }, { corps: { entrées: [] } }],
      'DELETE /instance/csp': { corps: { ok: true } },
    })
    render(<JournalCsp />)
    await screen.findByText('https://exfil.example')
    fireEvent.click(screen.getByText('Vider le journal'))
    await waitFor(() => expect(screen.getByText('Aucune violation signalée.')).toBeTruthy())
    expect(f.mock.calls.some(c => (c[1] as RequestInit | undefined)?.method === 'DELETE')).toBe(true)
  })
})
