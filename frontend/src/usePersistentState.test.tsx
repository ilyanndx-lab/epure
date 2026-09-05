import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'

import { usePersistentState } from './usePersistentState'

/**
 * `usePersistentState` quand `localStorage` REFUSE d'écrire.
 *
 * L'INCIDENT est de la même famille que celui de `ModuleBar.test.tsx`, et que
 * les deux corrigés le 2026-09-05 dans le module Code (`saveFile` marquant un
 * onglet propre après un 500, `openFile` ouvrant un onglet à `content`
 * indéfini) : **un échec réel, invisible.** Les deux écritures du hook — l'effet
 * debouncé et le flush `pagehide`/`beforeunload` — avalaient tout dans un
 * `catch { /* quota / privé : tant pis *\/ }`. Rien ne remontait, donc aucun
 * appelant ne pouvait savoir que sa valeur n'était PAS conservée.
 *
 * Ça compte plus depuis le même jour : le garde `beforeunload` du module Code
 * repose explicitement sur l'idée que ses onglets seront rejoués depuis
 * `localStorage`. Quand l'écriture échoue sur quota, l'utilisateur arbitre au
 * dialogue du navigateur — « quitter, ou rester ? » — avec une information
 * fausse.
 *
 * DEUX CAUSES, UN SEUL SENS. `QuotaExceededError` (stockage plein) et l'échec en
 * navigation privée signifient la même chose pour l'appelant : **non conservé**.
 * Le hook ne les distingue donc pas dans `ok` ; il expose seulement le NOM de
 * l'erreur, pour le diagnostic.
 *
 * CE QUE CES TESTS GARDENT : la signature reste destructurable à deux éléments
 * (le hook est partagé par une douzaine de composants), et l'état d'écriture dit
 * la vérité dans les deux sens — il repasse à `ok` quand l'écriture repasse.
 */

const CLE = 'test.persistance'

/** Sonde : affiche l'état d'écriture rendu par le hook. */
function Sonde({ valeur }: { valeur: string }) {
  const [v, setV, persistance] = usePersistentState<string>(CLE, 'initial')
  return (
    <div>
      <span data-testid="valeur">{v}</span>
      <span data-testid="ok">{String(persistance.ok)}</span>
      <span data-testid="erreur">{persistance.erreur ?? '-'}</span>
      <button onClick={() => setV(valeur)}>changer</button>
    </div>
  )
}

/** Consommateur écrit à l'ANCIENNE : deux éléments seulement. C'est la forme
 *  qu'utilisent App.tsx, Workshop.tsx, chat, docs… — elle ne doit pas casser. */
function SondeDeuxElements() {
  const [v] = usePersistentState<string>(CLE, 'initial')
  return <span data-testid="valeur">{v}</span>
}

function quotaDepasse(): Error {
  // Nom exact levé par les navigateurs quand le stockage est plein. Le hook ne
  // doit PAS le tester : il traite tout échec pareil. Il le RAPPORTE.
  const e = new Error('quota')
  e.name = 'QuotaExceededError'
  return e
}

describe('usePersistentState — échec de persistance', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
    cleanup()
  })

  /** Laisse passer le debounce de 400 ms de l'effet d'écriture. */
  const passerLeDebounce = () => act(() => { vi.advanceTimersByTime(500) })

  it('la destructuration à deux éléments continue de fonctionner', () => {
    localStorage.setItem(CLE, JSON.stringify('depuis le stockage'))
    render(<SondeDeuxElements />)
    expect(screen.getByTestId('valeur').textContent).toBe('depuis le stockage')
  })

  it('signale un échec de quota au lieu de l’avaler', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaDepasse()
    })

    render(<Sonde valeur="nouvelle" />)
    passerLeDebounce()

    expect(screen.getByTestId('ok').textContent).toBe('false')
    expect(screen.getByTestId('erreur').textContent).toBe('QuotaExceededError')
  })

  it('traite un échec de navigation privée exactement pareil', () => {
    // Certains navigateurs lèvent une erreur d'un autre nom, voire une valeur
    // qui n'est pas une Error du tout. Le sens pour l'appelant est identique :
    // non conservé. Aucune branche ne doit dépendre du nom.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw 'accès refusé en navigation privée'
    })

    render(<Sonde valeur="nouvelle" />)
    passerLeDebounce()

    expect(screen.getByTestId('ok').textContent).toBe('false')
    expect(screen.getByTestId('erreur').textContent).not.toBe('-')
  })

  it('ne signale rien quand l’écriture passe', () => {
    render(<Sonde valeur="nouvelle" />)
    passerLeDebounce()

    expect(screen.getByTestId('ok').textContent).toBe('true')
    expect(screen.getByTestId('erreur').textContent).toBe('-')
  })

  it('repasse à « conservé » dès qu’une écriture réussit', () => {
    // L'état doit dire la vérité dans les DEUX sens : un quota libéré (onglet
    // fermé ailleurs, historique vidé) ne doit pas laisser un bandeau d'alerte
    // à demeure.
    const espion = vi.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => { throw quotaDepasse() })

    const { rerender } = render(<Sonde valeur="a" />)
    passerLeDebounce()
    expect(screen.getByTestId('ok').textContent).toBe('false')

    espion.mockRestore()
    act(() => { screen.getByText('changer').click() })
    rerender(<Sonde valeur="a" />)
    passerLeDebounce()

    expect(screen.getByTestId('ok').textContent).toBe('true')
  })

  it('le flush de déchargement signale aussi son échec', () => {
    // Ce cas n'est pas théorique pour le module Code : son garde
    // `beforeunload` peut faire ANNULER la fermeture, la page continue donc de
    // vivre — et doit alors afficher la vérité sur ce qui a été conservé.
    render(<Sonde valeur="nouvelle" />)
    passerLeDebounce()
    expect(screen.getByTestId('ok').textContent).toBe('true')

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaDepasse()
    })
    act(() => { window.dispatchEvent(new Event('beforeunload')) })

    expect(screen.getByTestId('ok').textContent).toBe('false')
  })
})
