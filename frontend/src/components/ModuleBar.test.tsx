import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'

import ModuleBar from './ModuleBar'
import { chargerRecherche, reinitialiserRecherche } from '../recherche'

/**
 * ModuleBar face à des réponses qui n'ont pas la forme annoncée.
 *
 * L'INCIDENT. En production, dans le module Docs, à l'ouverture du panneau
 * fichiers puis à l'import d'un document :
 *
 *     TypeError: Cannot read properties of undefined (reading 'length')
 *         at Ce (ModuleBar-<hash>.js:3:5151)
 *
 * Cause : `GET /rag/files` répondait 500, et son corps d'erreur n'a pas de champ
 * `files`. Le code faisait
 *
 *     apiFetch(`${API}/rag/files`).then(r => r.json())
 *       .then((d: { files: string[] }) => setAvailableFiles(d.files))
 *
 * — où l'annotation est une AFFIRMATION que TypeScript croit sur parole. Sur un
 * corps `{"detail": "...", "type": "ImportError"}`, `d.files` vaut `undefined`,
 * `availableFiles` devient `undefined`, et le rendu suivant du panneau lit
 * `availableFiles.length`. Le `.catch(() => {})` ne voyait rien : `r.json()`
 * avait parfaitement réussi.
 *
 * Le 500 n'était pas un accident non plus. Mesuré sur la vraie app avec
 * `sentence_transformers` bloqué — c'est-à-dire la configuration d'un paquet
 * livré, où `HORS_PAQUET_PIP` l'exclut de l'installation :
 *
 *     GET /rag/files -> 500 {"detail":"Erreur interne du serveur","type":"ImportError"}
 *
 * `rag` est un `_LazyEngine` : le premier accès construit `RAGEngine`, donc un
 * `VectorStore`, qui importe `sentence_transformers` dans son `__init__`. Dans
 * un paquet, ce chemin lève à CHAQUE appel. Le panneau fichiers du module Docs
 * y était donc mort d'avance — et c'est ce corps de réponse exact qui est rejoué
 * ci-dessous.
 *
 * CE QUE CES TESTS GARDENT, et ce n'est pas « availableFiles » : aucune réponse
 * du backend ne doit être crue sur sa forme. Chaque état alimenté par un
 * `.json()` est éprouvé avec un corps d'erreur, parce que 500, 401 (token pas
 * encore appairé) et 404 (route absente d'une instance) sont tous des états
 * normaux d'une application locale dont le backend démarre en parallèle.
 */

/** Corps d'erreur du gestionnaire d'exceptions de `main.py`, mot pour mot. */
const ERREUR_500 = { detail: 'Erreur interne du serveur', type: 'ImportError' }

/**
 * `GET /rag/capabilities` — l'état de préparation du moteur documentaire.
 *
 * Ces corps sont recopiés de `core/embedding_install.py::_verdict`. Le cas
 * `en_cours` n'est pas une hypothèse : dans un paquet livré,
 * `sentence-transformers` n'est pas installé (il tire ~2 Go de torch), le
 * backend lance `pip` de lui-même et répond 503 en attendant. L'interface doit
 * dire ce qui se passe, pas rester vide plusieurs minutes.
 */
const CAPACITES_PRETES = {
  'état': 'prêt', disponible: true, message: 'Moteur de recherche documentaire prêt.',
  cause: '', 'taille_estimée_mo': 2000,
}
const CAPACITES_EN_COURS = {
  'état': 'en_cours', disponible: false,
  message: 'Préparation du moteur de recherche documentaire — téléchargement '
    + "d'environ 2 Go, quelques minutes, connexion réseau nécessaire.",
  cause: '', 'taille_estimée_mo': 2000,
}
const CAPACITES_ECHEC_RESEAU = {
  'état': 'échec', disponible: false,
  message: "Préparation impossible : l'index de téléchargement est injoignable. "
    + 'Vérifiez la connexion réseau, puis réessayez.',
  cause: 'réseau', 'taille_estimée_mo': 2000,
}

/** Le 503 que le backend rend pendant la préparation, corps compris. */
const ERREUR_503 = { detail: CAPACITES_EN_COURS.message, ...CAPACITES_EN_COURS }

/** `GET /context` nominal — l'état de session, sans lequel rien ne s'affiche. */
const CONTEXTE_OK = {
  fichiers_actifs: [],
  'résumé_contexte': '',
  'modèle_actif': 'qwen2.5:7b',
  strict_mode: false,
  session_instruction: '',
}

const MODELES_OK = {
  local: [{ id: 'qwen2.5:7b', nom: 'qwen2.5:7b', provider: 'ollama', disponible: true }],
  local_npu: [],
  cloud: { rapide: [], puissant: [], long_contexte: [] },
  fournisseurs: { gemini: false, groq: false },
  recommandations: {},
}

type Reponse = { status?: number; corps: unknown }

/**
 * Remplace `fetch` par une table URL → réponse.
 *
 * Le défaut est un 500 et non un 404 : on veut que toute route oubliée par un
 * test se comporte comme le pire cas réel, pas comme un silence.
 */
function poserFetch(table: Record<string, Reponse>) {
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : String(input)
    const cle = Object.keys(table).find(k => url.includes(k))
    const { status = 200, corps } = cle ? table[cle] : { corps: ERREUR_500, status: 500 }
    return new Response(JSON.stringify(corps), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', impl)
  return impl
}

/** Table nominale : tout répond, et bien. */
function tableSaine(): Record<string, Reponse> {
  return {
    '/pair': { corps: { token: 'jeton-de-test' } },
    '/context': { corps: CONTEXTE_OK },
    '/rag/files': { corps: { files: ['/fiches/cours.pdf'] } },
    '/rag/capabilities': { corps: CAPACITES_PRETES },
    '/models': { corps: MODELES_OK },
    '/voice/capabilities': {
      corps: {
        transcription: { disponible: true, manquants: [], raison: '' },
        'synthèse': { disponible: true, manquants: [], raison: '' },
      },
    },
    '/modules': { corps: { modules: [] } },
  }
}

/**
 * Rend la barre telle que le module Docs l'utilise, puis attend les effets.
 *
 * `module="docs" showFile showModel` est recopié de
 * `modules-catalogue/docs/Component.tsx` : c'est l'écran où l'incident s'est
 * produit, et les drapeaux décident quels `useEffect` partent.
 */
async function rendre() {
  const rendu = render(<ModuleBar module="docs" showFile showModel />)
  // Les trois fetch du montage résolvent hors du rendu initial : sans ce tour
  // de boucle, on testerait un composant qui n'a encore rien reçu, donc pas
  // l'état qui plante.
  await act(async () => { await Promise.resolve() })
  return rendu
}

/** Ouvre un panneau par le bouton de la barre (le `title` est son seul repère). */
async function ouvrir(titre: string) {
  const bouton = screen.getByTitle(titre)
  await act(async () => { bouton.click() })
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  // Le store de `recherche.ts` est un état de MODULE : il survit au démontage
  // (c'est son intérêt en production) et fuirait donc d'un test au suivant.
  reinitialiserRecherche()
})

describe('ModuleBar — panneau fichiers', () => {
  it('affiche les fichiers indexés quand /rag/files répond', async () => {
    poserFetch(tableSaine())
    await rendre()
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText('cours.pdf')).toBeTruthy())
  })

  it("s'ouvre sans planter quand /rag/files répond 500 — LE bug", async () => {
    // Le cas exact du paquet livré : sentence-transformers absent, donc 500 sur
    // toute route qui touche le RAG. Avant correction, l'ouverture du panneau
    // levait « Cannot read properties of undefined (reading 'length') ».
    poserFetch({ ...tableSaine(), '/rag/files': { status: 500, corps: ERREUR_500 } })
    await rendre()
    await ouvrir('Fichiers')
    // Le panneau existe, il est simplement vide : l'incapacité est honnête.
    expect(screen.getByText(/Glisser un fichier ici/)).toBeTruthy()
    expect(screen.queryByText('Fichiers indexés')).toBeNull()
  })

  it("survit à l'import d'un document quand /rag/files répond 500 ensuite", async () => {
    // Le geste rapporté : le panneau s'ouvre bien (le 500 n'arrive qu'après),
    // et c'est l'upload qui repose l'état à `undefined`.
    poserFetch(tableSaine())
    await rendre()
    await ouvrir('Fichiers')

    poserFetch({
      ...tableSaine(),
      '/files/upload': { corps: {} },
      '/rag/files': { status: 500, corps: ERREUR_500 },
    })
    const entree = document.querySelector('input[type="file"]') as HTMLInputElement
    const fichier = new File(['%PDF-1.4'], 'cours.pdf', { type: 'application/pdf' })
    Object.defineProperty(entree, 'files', { value: [fichier], configurable: true })
    await act(async () => {
      entree.dispatchEvent(new Event('change', { bubbles: true }))
      await Promise.resolve()
    })

    await waitFor(() => expect(screen.getByText(/Glisser un fichier ici/)).toBeTruthy())
  })

  it("annonce la préparation du moteur au lieu d'un panneau vide", async () => {
    // Le cas d'un paquet livré : /rag/files répond 503 pendant que le backend
    // installe torch + sentence-transformers. Avant, c'était un 500 et un
    // panneau vide sans explication — l'utilisateur ne pouvait pas savoir qu'il
    // fallait attendre, ni combien.
    poserFetch({
      ...tableSaine(),
      '/rag/files': { status: 503, corps: ERREUR_503 },
      '/rag/capabilities': { corps: CAPACITES_EN_COURS },
    })
    await rendre()
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText(/Préparation du moteur/)).toBeTruthy())
    // Le message dit le poids ET que le réseau est nécessaire : ce sont les deux
    // seules choses que l'utilisateur peut vérifier de son côté.
    expect(screen.getByText(/2 Go/)).toBeTruthy()
    expect(screen.getByText(/connexion réseau/)).toBeTruthy()
    // Une préparation n'est pas un échec : pas de bouton « Réessayer ».
    expect(screen.queryByText('Réessayer')).toBeNull()
    // Et le panneau reste utilisable, sans « Fichiers indexés » mensonger.
    expect(screen.getByText(/Glisser un fichier ici/)).toBeTruthy()
    expect(screen.queryByText('Fichiers indexés')).toBeNull()
  })

  it("distingue un échec réseau d'une préparation, et propose de réessayer", async () => {
    poserFetch({
      ...tableSaine(),
      '/rag/files': { status: 503, corps: ERREUR_503 },
      '/rag/capabilities': { corps: CAPACITES_ECHEC_RESEAU },
      '/rag/install': { corps: CAPACITES_EN_COURS },
    })
    await rendre()
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText(/index de téléchargement est injoignable/)).toBeTruthy())
    // Le backend ne réessaie pas tout seul (une tentative par process) : sans ce
    // bouton, l'échec resterait affiché jusqu'au prochain démarrage.
    const bouton = screen.getByText('Réessayer')
    await act(async () => { bouton.click() })
    await waitFor(() => expect(screen.getByText(/Préparation du moteur/)).toBeTruthy())
    expect(screen.queryByText('Réessayer')).toBeNull()
  })

  it('remplit le panneau tout seul quand le moteur devient prêt', async () => {
    // La fin de l'histoire, et le seul point qui rende l'attente supportable :
    // après plusieurs minutes d'installation, l'utilisateur ne doit pas avoir à
    // fermer et réouvrir l'écran.
    poserFetch({
      ...tableSaine(),
      '/rag/files': { status: 503, corps: ERREUR_503 },
      '/rag/capabilities': { corps: CAPACITES_EN_COURS },
    })
    await rendre()
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText(/Préparation du moteur/)).toBeTruthy())
    expect(screen.queryByText('cours.pdf')).toBeNull()

    // L'installation aboutit. `chargerRecherche()` est appelé à la main plutôt
    // que d'attendre l'interrogation périodique : c'est exactement ce que fait
    // la minuterie de `recherche.ts`, et un test ne doit pas coûter 4 secondes.
    poserFetch(tableSaine())
    await act(async () => { await chargerRecherche() })

    await waitFor(() => expect(screen.getByText('cours.pdf')).toBeTruthy())
    expect(screen.queryByText(/Préparation du moteur/)).toBeNull()
  })

  it('ne dit rien quand /rag/capabilities est absente (backend plus ancien)', async () => {
    // Défaut inverse de `voix.ts`, et c'est voulu : ici l'incertitude doit être
    // SILENCIEUSE. Afficher « préparation en cours » par défaut mettrait un
    // bandeau anxiogène sur une installation parfaitement saine.
    poserFetch({ ...tableSaine(), '/rag/capabilities': { status: 404, corps: { detail: 'Not Found' } } })
    await rendre()
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText('cours.pdf')).toBeTruthy())
    expect(screen.queryByText(/Préparation du moteur/)).toBeNull()
    expect(screen.queryByText('Réessayer')).toBeNull()
  })

  it('propose les types de documents que le backend sait lire', async () => {
    // La moitié VISIBLE du support de format : `accept` décide de ce que le
    // sélecteur de fichiers propose. Il était écrit à la main, en double avec le
    // filtre de `uploadFiles` — deux listes pour une notion, donc un ajout qui
    // s'oublie d'un côté. Désaccordées, elles produisent un fichier qu'on peut
    // choisir et qui disparaît sans message.
    //
    // Miroir de `SUPPORTED_EXTENSIONS` dans `backend/core/rag.py` : ce test ne
    // peut pas vérifier l'accord entre les deux dépôts, mais il verrouille le
    // fait que les formats bureautiques y sont — c'est ce qui a été ajouté, et
    // c'est ce qu'un `accept` réécrit à la main perdrait en premier.
    poserFetch(tableSaine())
    await rendre()
    await ouvrir('Fichiers')
    const entree = document.querySelector('input[type="file"]') as HTMLInputElement
    const accept = entree.getAttribute('accept') ?? ''
    for (const ext of ['.pdf', '.docx', '.pptx', '.xlsx', '.txt', '.md', '.csv', '.json']) {
      expect(accept.split(',')).toContain(ext)
    }
    // Les formats binaires pré-2007 ne sont lus par aucune des bibliothèques :
    // les proposer donnerait une erreur à l'ouverture au lieu d'un refus honnête.
    for (const ext of ['.doc', '.ppt', '.xls']) {
      expect(accept.split(',')).not.toContain(ext)
    }
  })

  it('reste rendu quand TOUT le backend répond 500', async () => {
    // Backend qui démarre, token pas encore appairé, route absente : la barre
    // doit s'afficher amputée, pas disparaître derrière une ErrorBoundary.
    poserFetch({})
    await rendre()
    await ouvrir('Fichiers')
    expect(screen.getByText(/Glisser un fichier ici/)).toBeTruthy()
  })
})

describe('ModuleBar — panneau modèles', () => {
  it('liste les modèles quand /models répond', async () => {
    poserFetch(tableSaine())
    await rendre()
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    // `getAllByText` et non `getByText` : le nom du modèle apparaît deux fois,
    // dans la liste et dans le libellé à droite de la barre. Le second existe
    // même sans /models (il vient de /context) — n'assurer que sa présence ne
    // prouverait rien.
    await waitFor(() => expect(screen.getByText('Local')).toBeTruthy())
    expect(screen.getAllByText('qwen2.5:7b').length).toBeGreaterThan(1)
  })

  it("s'ouvre sans planter quand /models répond 500", async () => {
    // Cette réponse-là a changé de forme récemment (filtrage des fournisseurs
    // cloud sans clé, ajout de `fournisseurs`) : le panneau doit tenir sur un
    // corps qui n'a ni `local`, ni `cloud`, ni `fournisseurs`.
    poserFetch({ ...tableSaine(), '/models': { status: 500, corps: ERREUR_500 } })
    await rendre()
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    expect(screen.getByText(/Chargement des modèles/)).toBeTruthy()
  })

  it('tient sur un /models dont les catégories cloud manquent', async () => {
    // `cloud: {}` est TRUTHY : un `?? {rapide: [], …}` ne le rattrape pas, et
    // `[...cloudCategories.rapide]` échouerait sur « n'est pas itérable ».
    poserFetch({
      ...tableSaine(),
      '/models': { corps: { local: [], cloud: {}, recommandations: {} } },
    })
    await rendre()
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    expect(screen.getByText(/Chargement des modèles/)).toBeTruthy()
  })
})
