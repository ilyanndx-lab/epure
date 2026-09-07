import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'

import EncreModule from './Component'

/**
 * Le module « encre » face à des réponses qui n'ont pas la forme annoncée.
 *
 * CE QUE CE FICHIER GARDE, et ce n'est pas « la liste des pages » : aucune
 * réponse du backend ne doit être crue sur sa forme. `r.json() as {pages: …}`
 * est une AFFIRMATION que TypeScript croit sur parole, et 500 (gestionnaire
 * d'exceptions de `main.py`), 401 (token pas encore appairé) et 404 (instance
 * qui n'a pas ce module) sont tous des états NORMAUX d'une application locale
 * dont le backend démarre en parallèle du front. Le champ annoncé vaut alors
 * `undefined`, l'état devient `undefined`, le `.catch()` ne voit rien — puisque
 * `r.json()` a parfaitement réussi — et la faute n'apparaît qu'au rendu suivant,
 * sur un `.length`, dans un chunk minifié dont la trace ne nomme même pas la
 * ligne. C'est l'incident qui a fait naître `ModuleBar.test.tsx` ; ce fichier en
 * reprend l'idiome, y compris le détail du bouchon expliqué plus bas.
 *
 * ── Deux prothèses, pour deux manques réels de jsdom ─────────────────────────
 *
 * `ResizeObserver` n'y existe pas, et `HTMLCanvasElement.getContext` y rend
 * `null` (le paquet `canvas` n'est pas installé, et l'installer ferait entrer
 * une extension compilée pour tester un dessin que personne ne regarde). Le
 * composant traite les deux comme des cas dégradés — c'est-à-dire qu'il rend une
 * page sans dessiner — et c'est exactement ce qu'on veut éprouver : un module
 * dont le rendu jette casse toute l'application, `ModuleErrorBoundary` n'attrapant
 * ni l'asynchrone ni les gestionnaires d'événements.
 */

/** Corps d'erreur du gestionnaire d'exceptions de `main.py`, mot pour mot. */
const ERREUR_500 = { detail: 'Erreur interne du serveur', type: 'ImportError' }

/** Le 404 d'une instance où le module n'est pas monté, tel que FastAPI le rend. */
const ERREUR_404 = { detail: 'Not Found' }

/** `GET /encre/pages` nominal. */
const PAGES_OK = {
  pages: [
    {
      id: 'a1b2', titre: 'Mécanique du point',
      date_creation: '2026-09-07T10:00:00', date_modification: '2026-09-07T10:30:00',
      n_traits: 12,
    },
  ],
}

type Reponse = { status?: number; corps: unknown }

/**
 * Remplace `fetch` par une table URL → réponse.
 *
 * Le défaut est un 500 et non un 404 : une route oubliée par un test doit se
 * comporter comme le pire cas réel, pas comme un silence.
 *
 * ⚠️ La clé retenue est la PLUS LONGUE qui corresponde, jamais la première
 * trouvée — recopié de `ModuleBar.test.tsx`, et pas par mimétisme : ici
 * `/encre/pages` est un préfixe de `/encre/pages/a1b2`, donc un `find` ferait
 * répondre la LISTE à la lecture d'une page. Le test échouerait alors en
 * décrivant un composant qui marche, sur une route qu'il n'a jamais consultée.
 */
function poserFetch(table: Record<string, Reponse>) {
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : String(input)
    const cle = Object.keys(table)
      .filter(k => url.includes(k))
      .sort((a, b) => b.length - a.length)[0]
    const { status = 200, corps } = cle ? table[cle] : { corps: ERREUR_500, status: 500 }
    return new Response(JSON.stringify(corps), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', impl)
  return impl
}

/**
 * `/pair` est TOUJOURS bouchonné : `apiFetch` l'appelle de lui-même quand aucun
 * token n'est en `localStorage`, et le laisser tomber sur le 500 par défaut
 * ajouterait un appel raté à chaque test — donc du bruit dans ce qu'on mesure.
 */
function table(reste: Record<string, Reponse>): Record<string, Reponse> {
  return { '/pair': { corps: { token: 'jeton-de-test' } }, ...reste }
}

/** Rend le module et laisse les effets du montage se résoudre. */
async function rendre(t: Record<string, Reponse>) {
  poserFetch(table(t))
  const rendu = render(<EncreModule />)
  // Sans ce tour de boucle, on testerait un composant qui n'a encore rien reçu,
  // donc pas l'état qui plante.
  await act(async () => { await Promise.resolve() })
  return rendu
}

// `globals: false` dans vitest.config.ts : le nettoyage automatique de
// testing-library n'est PAS branché. Sans cet appel, chaque rendu s'ajoute au
// document et `getByText` échoue en « plusieurs éléments trouvés » — sur un
// composant parfaitement sain. Même idiome que `ModuleBar.test.tsx`.
afterEach(cleanup)

beforeEach(() => {
  localStorage.clear()
  // jsdom n'implémente pas ResizeObserver. Bouchon minimal : le composant s'en
  // sert pour repeindre à chaque redimensionnement, ce qui n'a aucun sens sans
  // mise en page. Le composant sait aussi s'en passer (il teste sa présence) ;
  // le poser ici éprouve malgré tout le chemin nominal.
  vi.stubGlobal('ResizeObserver', class {
    observe() { /* rien à observer sans mise en page */ }
    unobserve() { /* idem */ }
    disconnect() { /* idem */ }
  })
})

describe('module encre — frontières .json()', () => {
  it("affiche une liste vide plutôt que de planter sur un corps d'erreur 500", async () => {
    // LE cas de ce fichier : `GET /encre/pages` répond 500, son corps n'a pas de
    // champ `pages`, et un `d.pages` cru sur parole vaudrait `undefined`.
    await rendre({ '/encre/pages': { status: 500, corps: ERREUR_500 } })
    expect(screen.getByText(/Aucune page/)).toBeTruthy()
  })

  it('survit à un 404 — une instance où le module n\'est pas monté', async () => {
    await rendre({ '/encre/pages': { status: 404, corps: ERREUR_404 } })
    expect(screen.getByText(/Aucune page/)).toBeTruthy()
  })

  it('survit à un corps 200 dont le champ `pages` a le mauvais type', async () => {
    // Le cas le plus vicieux, parce que rien n'échoue : statut 200, JSON valide,
    // `.catch()` jamais appelé. Seule la normalisation peut dire non.
    for (const corps of [{ pages: null }, { pages: 'deux pages' }, { pages: {} }, {}]) {
      const { unmount } = await rendre({ '/encre/pages': { corps } })
      expect(screen.getByText(/Aucune page/)).toBeTruthy()
      unmount()
    }
  })

  it('écarte une entrée de liste sans identifiant au lieu de l\'afficher', async () => {
    // Une entrée sans `id` serait affichée, cliquable, et mènerait à un 404 que
    // l'utilisateur ne saurait pas lire. On préfère ne pas la montrer.
    await rendre({
      '/encre/pages': { corps: { pages: [{ titre: 'Fantôme' }, PAGES_OK.pages[0]] } },
    })
    expect(screen.queryByText('Fantôme')).toBeNull()
    expect(screen.getByText('Mécanique du point')).toBeTruthy()
  })

  it('affiche la liste nominale', async () => {
    // Contrôle du contrôle : sans lui, les tests ci-dessus resteraient verts si
    // le composant n'affichait JAMAIS de page.
    await rendre({ '/encre/pages': { corps: PAGES_OK } })
    expect(screen.getByText('Mécanique du point')).toBeTruthy()
    expect(screen.getByText(/12 traits/)).toBeTruthy()
  })

  it('rend sans canvas quand jsdom ne fournit aucun contexte 2D', async () => {
    // `getContext('2d')` rend `null` ici. Le composant doit rendre l'éditeur
    // quand même — un module qui jette au rendu casse toute l'application.
    await rendre({
      '/encre/pages': { corps: PAGES_OK },
      '/encre/pages/a1b2': {
        corps: { id: 'a1b2', titre: 'Mécanique du point', strokes: [] },
      },
    })
    const entree = screen.getByText('Mécanique du point')
    await act(async () => { entree.click() })
    await act(async () => { await Promise.resolve() })
    expect(screen.getByLabelText('Titre de la page')).toBeTruthy()
  })

  it("quitte la page au lieu d'afficher un canvas vide quand son chargement échoue", async () => {
    // Le point n'est pas cosmétique : une page blanche laissée à l'écran serait
    // enregistrée par-dessus le fichier réel à la première modification. On
    // revient donc à l'écran d'accueil, avec le message du backend.
    await rendre({
      '/encre/pages': { corps: PAGES_OK },
      '/encre/pages/a1b2': { status: 500, corps: ERREUR_500 },
    })
    const entree = screen.getByText('Mécanique du point')
    await act(async () => { entree.click() })
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByLabelText('Titre de la page')).toBeNull()
    expect(screen.getByRole('status').textContent).toContain('Erreur interne du serveur')
  })

  it('affiche le `detail` du backend, jamais « [object Object] »', async () => {
    // Une erreur de validation FastAPI met une LISTE d'objets dans `detail` :
    // l'afficher brute donnerait « [object Object] ». On vérifie le TYPE.
    await rendre({
      '/encre/pages': { corps: PAGES_OK },
      '/encre/pages/a1b2': {
        status: 422,
        corps: { detail: [{ loc: ['body'], msg: 'champ manquant', type: 'missing' }] },
      },
    })
    const entree = screen.getByText('Mécanique du point')
    await act(async () => { entree.click() })
    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('status').textContent).not.toContain('[object Object]')
    expect(screen.getByRole('status').textContent).toContain('422')
  })

  it('ne plante pas sur des traits dont les points sont mal formés', async () => {
    // Le backend traite les tracés comme OPAQUES (`core/encre.py`) : il stockera
    // volontiers ce qu'un client d'une autre version lui envoie. C'est le rendu
    // qui doit tenir — un `undefined` passé à `getStroke` produirait des `NaN`,
    // donc une page blanche dont le fichier est pourtant intact.
    await rendre({
      '/encre/pages': { corps: PAGES_OK },
      '/encre/pages/a1b2': {
        corps: {
          id: 'a1b2', titre: 'Mécanique du point',
          strokes: [
            { points: [{ x: 'douze', y: null }] },
            { points: 'pas une liste' },
            null,
            { points: [{ x: 1, y: 2, pression: 0.5, t: 0 }] },
          ],
        },
      },
    })
    const entree = screen.getByText('Mécanique du point')
    await act(async () => { entree.click() })
    await act(async () => { await Promise.resolve() })
    expect(screen.getByLabelText('Titre de la page')).toBeTruthy()
  })
})
