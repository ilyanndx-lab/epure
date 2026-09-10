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
 * `VectorStore`, qui construisait alors `sentence_transformers` dans son
 * `__init__`. Dans un paquet, ce chemin levait à CHAQUE appel. Le panneau
 * fichiers du module Docs y était donc mort d'avance — et c'est ce corps de
 * réponse exact qui est rejoué ci-dessous.
 *
 * (La pile a changé le 2026-08-26 — ONNX Runtime, modèle téléchargé au premier
 * usage — mais le 500 rejoué ici reste le bon cas de test : ce que ce fichier
 * garde est la NORMALISATION à la frontière `.json()`, qui ne dépend d'aucune
 * pile. N'importe quelle route peut répondre 500 ou 401 sur une app locale dont
 * le backend démarre en parallèle.)
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
 * Ces corps sont recopiés de `core/embedding_install.py::_verdict` — mis à jour
 * le 2026-08-26, quand la pile est passée de `pip install torch` +
 * `sentence-transformers` (~2 Go) au téléchargement des 90 Mo d'un modèle ONNX.
 * Les recopier plutôt que les deviner est tout l'intérêt de ce fichier : le
 * bandeau affiche le `message` du backend tel quel, donc un corps inventé
 * testerait une phrase que personne n'envoie.
 *
 * Le cas `en_cours` n'est pas une hypothèse : dans un paquet livré, le modèle
 * n'est pas encore là, le backend le télécharge de lui-même et répond 503 en
 * attendant. L'interface doit dire ce qui se passe, pas rester vide.
 */
const CAPACITES_PRETES = {
  'état': 'prêt', disponible: true, message: 'Moteur de recherche documentaire prêt.',
  cause: '', 'taille_estimée_mo': 91,
}
const CAPACITES_EN_COURS = {
  'état': 'en_cours', disponible: false,
  message: 'Préparation du moteur de recherche documentaire — téléchargement du '
    + 'modèle (91 Mo), une à deux minutes, connexion réseau nécessaire.',
  cause: '', 'taille_estimée_mo': 91,
}
const CAPACITES_ECHEC_RESEAU = {
  'état': 'échec', disponible: false,
  message: 'Préparation impossible : le serveur du modèle est injoignable. '
    + 'Vérifiez la connexion réseau, puis réessayez.',
  cause: 'réseau', 'taille_estimée_mo': 91,
}

/**
 * État du modèle vision, tel que rendu par `GET /rag/capabilities` depuis son
 * champ `vision` — invisible ailleurs que dans les logs backend avant cet
 * ajout. Recopié de `core/models.py::premier_modele_vision_disponible` : ces
 * deux formes sont les deux seules que la route puisse produire.
 */
const CAPACITES_VISION_OLLAMA = {
  ...CAPACITES_PRETES,
  vision: { disponible: true, modele: 'moondream:latest', source: 'ollama' },
}
const CAPACITES_VISION_ABSENTE = {
  ...CAPACITES_PRETES,
  vision: { disponible: false, modele: null, source: null },
}

/** Le 503 que le backend rend pendant la préparation, corps compris. */
const ERREUR_503 = { detail: CAPACITES_EN_COURS.message, ...CAPACITES_EN_COURS }

/** `GET /context` nominal — l'état de session, sans lequel rien ne s'affiche.
 *
 * Ne porte plus `fichiers_actifs` ni `résumé_contexte` : ces clés ont été
 * retirées le 2026-08-27 et les fichiers appartiennent désormais à la
 * conversation (docs/conversations-persistees.md §2). Les laisser ici ferait
 * croire que le composant les lit encore.
 */
const CONTEXTE_OK = {
  'modèle_actif': 'qwen2.5:7b',
  strict_mode: false,
  'instruction_générale': '',
}

/** `GET /chat/conversations/{id}` nominal, fichiers marqués `présent`. */
const CONVERSATION_OK = {
  id: 'conv-1', titre: 'Thermo', messages: [],
  // Le MEME chemin que `/rag/files` : le panneau liste le corpus indexé et coche
  // ce qui est attaché. Un attaché hors corpus ne s'afficherait pas du tout.
  'fichiers_attachés': [{ chemin: '/fiches/cours.pdf', 'présent': true }],
  corpus_interrogeable: true,
}

/** Les cases du panneau fichiers, dans l'ordre du corpus. */
function cases(): HTMLInputElement[] {
  return Array.from(document.querySelectorAll('input[type="checkbox"]'))
}

const MODELES_OK = {
  local: [{ id: 'qwen2.5:7b', nom: 'qwen2.5:7b', provider: 'ollama', disponible: true }],
  local_npu: [],
  local_lmstudio: [],
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
 *
 * La clé retenue est la PLUS LONGUE qui corresponde, jamais la première trouvée.
 * Avec `find`, `/models/loaded` tombait sur l'entrée `/models` — déclarée avant
 * dans `tableSaine` — et recevait le corps de la liste des modèles : le test
 * échouait en décrivant un composant qui marchait, sur une route qu'il n'avait
 * jamais consultée. Un bouchon qui répond à côté est pire qu'un bouchon absent.
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
async function rendre(conversationId = '') {
  const rendu = render(
    <ModuleBar module="docs" conversationId={conversationId} showFile showModel />)
  // Les trois fetch du montage résolvent hors du rendu initial : sans ce tour
  // de boucle, on testerait un composant qui n'a encore rien reçu, donc pas
  // l'état qui plante.
  await act(async () => { await Promise.resolve() })
  return rendu
}

/**
 * Rend la barre telle que le module Chat l'utilise (seul consommateur de
 * `showSkills` en production, `Component.tsx:1985-1991`), et ouvre directement
 * le panneau « Paramètres de session » où vit le toggle de réflexion.
 */
async function rendreCompetences(table: Record<string, Reponse>) {
  poserFetch(table)
  const rendu = render(<ModuleBar module="chat" conversationId="" showFile showModel showSkills />)
  await act(async () => { await Promise.resolve() })
  await ouvrir('Paramètres de session')
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
    // Le cas exact du paquet livré : moteur d'embedding pas encore prêt, donc 500
    // sur toute route qui touche le RAG. Avant correction, l'ouverture du panneau
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
    // télécharge le modèle. Avant, c'était un 500 et un panneau vide sans
    // explication — l'utilisateur ne pouvait pas savoir qu'il fallait attendre,
    // ni combien.
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
    expect(screen.getByText(/91 Mo/)).toBeTruthy()
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
    await waitFor(() => expect(screen.getByText(/serveur du modèle est injoignable/)).toBeTruthy())
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

  it('annonce le modèle vision détecté', async () => {
    poserFetch({ ...tableSaine(), '/rag/capabilities': { corps: CAPACITES_VISION_OLLAMA } })
    await rendre()
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText(/Vision : moondream:latest détecté \(Ollama\)/)).toBeTruthy())
  })

  it("propose d'installer un modèle vision quand aucun n'est détecté", async () => {
    poserFetch({ ...tableSaine(), '/rag/capabilities': { corps: CAPACITES_VISION_ABSENTE } })
    await rendre()
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText(/aucun modèle détecté/)).toBeTruthy())
  })

  it('ne dit rien sur la vision quand /rag/capabilities est absente (backend plus ancien)', async () => {
    // Même garde-fou que le bandeau de préparation juste au-dessus, et pour la
    // même raison : `état: 'inconnu'` ne doit jamais s'afficher comme un
    // verdict négatif — ici « aucun modèle détecté » sur un poste qui en a un.
    poserFetch({ ...tableSaine(), '/rag/capabilities': { status: 404, corps: { detail: 'Not Found' } } })
    await rendre()
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText('cours.pdf')).toBeTruthy())
    expect(screen.queryByText(/Vision :/)).toBeNull()
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

  it('coche les fichiers attachés À CETTE conversation', async () => {
    poserFetch({ ...tableSaine(), '/chat/conversations/conv-1': { corps: CONVERSATION_OK } })
    await rendre('conv-1')
    await ouvrir('Fichiers')
    await waitFor(() => expect(cases().some(c => c.checked)).toBe(true))
  })

  it("s'ouvre sans planter quand la conversation répond 404", async () => {
    // Supprimée depuis un autre onglet : le corps est `{"detail": …}`, donc sans
    // `fichiers_attachés`. Un `as` le laisserait passer jusqu'au `.map()`.
    poserFetch({
      ...tableSaine(),
      '/chat/conversations/conv-1': { status: 404, corps: { detail: 'Conversation introuvable' } },
    })
    await rendre('conv-1')
    await ouvrir('Fichiers')
    expect(screen.getByTitle('Fichiers')).toBeTruthy()
  })

  it("tient quand `fichiers_attachés` n'est pas un tableau", async () => {
    // 200 avec un corps inattendu — le cas que `Array.isArray` attrape et que
    // `?? []` laisserait passer (une chaîne est itérable caractère par caractère).
    poserFetch({
      ...tableSaine(),
      '/chat/conversations/conv-1': { corps: { id: 'conv-1', 'fichiers_attachés': 'oups' } },
    })
    await rendre('conv-1')
    await ouvrir('Fichiers')
    expect(screen.getByTitle('Fichiers')).toBeTruthy()
  })

  it('sans conversation ouverte, le corpus reste visible mais rien n’est coché', async () => {
    // Il n'y a rien à quoi attacher, mais le corpus indexé doit rester
    // consultable — et surtout aucun fichier ne doit apparaître comme attaché,
    // ce qui reviendrait à montrer les fichiers d'un autre fil.
    poserFetch(tableSaine())
    await rendre('')
    await ouvrir('Fichiers')
    await waitFor(() => expect(screen.getByText('cours.pdf')).toBeTruthy())
    expect(cases().some(c => c.checked)).toBe(false)
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

  it('liste un modèle LM Studio sous sa propre section', async () => {
    poserFetch({
      ...tableSaine(),
      '/models': {
        corps: {
          ...MODELES_OK,
          local_lmstudio: [{
            id: 'lmstudio:llama-3.1-8b-instruct', nom: 'llama-3.1-8b-instruct',
            provider: 'lmstudio', disponible: true,
          }],
        },
      },
    })
    await rendre()
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    await waitFor(() => expect(screen.getByText('Local LM Studio')).toBeTruthy())
    expect(screen.getByText('llama-3.1-8b-instruct')).toBeTruthy()
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

/**
 * Toggle de réflexion (panneau « Paramètres de session ») — honnête sur ce
 * qu'il fait vraiment selon le PROVIDER du modèle actif, pas seulement sur
 * son propre état coché/décoché.
 *
 * `core/llm.py::stream` traite trois cas très différents derrière le même
 * bool `raisonnement` : `gemini` l'ignore intégralement (aucune bascule
 * n'existe côté SDK), les cinq fournisseurs OpenAI-compatibles cloud
 * (groq/cerebras/mistral/nvidia/deepseek) ne l'utilisent que pour relever un
 * plafond de tokens sans jamais faire remonter de réflexion visible, et seuls
 * `ollama`/`flm` en affichent une véritable. Avant cette correction, le même
 * texte (« Sa réflexion s'affiche pendant l'attente ») s'affichait dans les
 * trois cas — faux pour cinq fournisseurs sur sept, et pour gemini le toggle
 * acceptait une valeur qui ne produit STRICTEMENT aucun effet.
 */
const MODELES_GEMINI = {
  local: [], local_npu: [], local_lmstudio: [],
  cloud: {
    rapide: [{ id: 'gemini:gemini-2.5-flash', nom: 'Gemini 2.5 Flash', provider: 'gemini', disponible: true }],
    puissant: [], long_contexte: [],
  },
  fournisseurs: {}, recommandations: {},
}
const MODELES_GROQ = {
  local: [], local_npu: [], local_lmstudio: [],
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
  cloud: { rapide: [], puissant: [], long_contexte: [] },
  fournisseurs: {}, recommandations: {},
}

describe('ModuleBar — toggle de réflexion, honnête selon le provider', () => {
  it("masque le toggle et remplace par un texte explicite quand gemini est actif — aucun effet à annoncer", async () => {
    await rendreCompetences({
      ...tableSaine(),
      '/context': { corps: { ...CONTEXTE_OK, 'modèle_actif': 'gemini:gemini-2.5-flash' } },
      '/models': { corps: MODELES_GEMINI },
    })
    await waitFor(() => expect(screen.getByText(/non disponible sur Gemini/)).toBeTruthy())
    // Aucun contrôle qui accepterait une valeur sans effet, sous aucun libellé.
    expect(screen.queryByRole('switch', { name: 'Réflexion du modèle' })).toBeNull()
    expect(screen.queryByRole('switch', { name: 'Budget de réflexion (tokens)' })).toBeNull()
  })

  it("relabellise en « budget de réflexion » sur un fournisseur cloud qui ne montre jamais sa pensée (groq)", async () => {
    await rendreCompetences({
      ...tableSaine(),
      '/context': { corps: { ...CONTEXTE_OK, 'modèle_actif': 'groq:openai/gpt-oss-20b' } },
      '/models': { corps: MODELES_GROQ },
    })
    // Le toggle reste un vrai contrôle (il agit sur le plafond de tokens) mais
    // ne prétend plus à une réflexion visible que groq ne produit jamais.
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Budget de réflexion (tokens)' })).toBeTruthy())
    expect(screen.getByText(/relève seulement le plafond de tokens/)).toBeTruthy()
    expect(screen.queryByText(/Sa réflexion s'affiche pendant l'attente/)).toBeNull()
  })

  it('garde le libellé et le texte actuels sur ollama — non-régression', async () => {
    await rendreCompetences(tableSaine())
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Réflexion du modèle' })).toBeTruthy())
    expect(screen.getByText(/Sa réflexion s'affiche pendant l'attente/)).toBeTruthy()
  })

  it("relabellise en « budget de réflexion » sur LM Studio — aucun paramètre stable côté serveur", async () => {
    await rendreCompetences({
      ...tableSaine(),
      '/context': { corps: { ...CONTEXTE_OK, 'modèle_actif': 'lmstudio:llama-3.1-8b-instruct' } },
      '/models': { corps: MODELES_LMSTUDIO },
    })
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Budget de réflexion (tokens)' })).toBeTruthy())
    expect(screen.getByText(/relève seulement le plafond de tokens/)).toBeTruthy()
    expect(screen.queryByText(/Sa réflexion s'affiche pendant l'attente/)).toBeNull()
  })
})

/**
 * Modèles Ollama résidents en mémoire (`GET /models/loaded`).
 *
 * La distinction qui porte tout : `charges: null` veut dire « Ollama ne répond
 * pas », `charges: []` veut dire « Ollama répond et rien n'est chargé ». Les
 * confondre afficherait « injoignable » sur une machine au repos parfaitement
 * saine — ou proposerait un bouton « charger » là où il n'y a pas de serveur.
 *
 * L'état est lu À L'OUVERTURE du panneau, sans action : c'est une exigence, pas
 * un détail — un état qu'il faut demander n'est pas un état visible.
 */
describe('ModuleBar — mémoire Ollama', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  const ouvrirListe = async () => {
    await rendre()
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    await waitFor(() => expect(screen.getByText('Local')).toBeTruthy())
  }

  it('rien de chargé : le bouton propose « charger », sans mention de mémoire', async () => {
    poserFetch({ ...tableSaine(), '/models/loaded': { corps: { charges: [] } } })
    await ouvrirListe()
    await waitFor(() => expect(screen.getByText('charger')).toBeTruthy())
    expect(screen.queryByText('en mémoire')).toBeNull()
  })

  it('modèle chargé : il est annoncé « en mémoire » et le bouton propose « libérer »', async () => {
    poserFetch({
      ...tableSaine(),
      '/models/loaded': { corps: { charges: [{ id: 'qwen2.5:7b', processeur: 'cpu' }] } },
    })
    await ouvrirListe()
    await waitFor(() => expect(screen.getByText('en mémoire')).toBeTruthy())
    expect(screen.getByText('libérer')).toBeTruthy()
    expect(screen.queryByText('charger')).toBeNull()
  })

  it('Ollama absent : aucun bouton, aucune alerte — le cas nominal se tait', async () => {
    // `charges: null` et non une liste vide : c'est la réponse du backend quand
    // Ollama ne répond pas, et elle NE DOIT PAS virer au rouge.
    poserFetch({ ...tableSaine(), '/models/loaded': { corps: { charges: null } } })
    await ouvrirListe()
    expect(screen.queryByText('charger')).toBeNull()
    expect(screen.queryByText('libérer')).toBeNull()
    expect(screen.queryByText('en mémoire')).toBeNull()
  })

  it('la route absente est traitée comme Ollama absent, pas comme une panne', async () => {
    // Une instance plus ancienne n'a pas cette route : 404. Le panneau doit se
    // comporter comme sans Ollama, pas afficher une erreur.
    poserFetch({ ...tableSaine(), '/models/loaded': { status: 404, corps: { detail: 'Not Found' } } })
    await ouvrirListe()
    expect(screen.queryByText('charger')).toBeNull()
    expect(screen.getAllByText('qwen2.5:7b').length).toBeGreaterThan(0)
  })

  it('une éjection sans effet affiche SON message et laisse le modèle chargé', async () => {
    // Le cas mesuré : une génération tient le modèle, Ollama répond 200
    // `unload` et ne libère rien. Le backend relit et renvoie `ok: false` avec
    // le modèle toujours dans `charges`. Croire le succès afficherait
    // « libéré » puis un retour inexpliqué.
    poserFetch({
      ...tableSaine(),
      '/models/loaded': { corps: { charges: [{ id: 'qwen2.5:7b' }] } },
      '/models/unload': {
        corps: {
          ok: false,
          message: '« qwen2.5:7b » est toujours chargé : une génération l\'utilise sans doute.',
          charges: [{ id: 'qwen2.5:7b' }],
        },
      },
    })
    await ouvrirListe()
    await waitFor(() => expect(screen.getByText('libérer')).toBeTruthy())

    await act(async () => { screen.getByText('libérer').click() })

    await waitFor(() => expect(screen.getByText(/toujours chargé/)).toBeTruthy())
    expect(screen.getByText('en mémoire')).toBeTruthy()
    expect(screen.getByText('libérer')).toBeTruthy()
  })

  it('un corps sans `charges` ne casse pas le rendu', async () => {
    // §8 : aucune réponse n'est crue sur sa forme.
    poserFetch({ ...tableSaine(), '/models/loaded': { corps: { detail: 'autre chose' } } })
    await ouvrirListe()
    expect(screen.getAllByText('qwen2.5:7b').length).toBeGreaterThan(0)
  })
})

/**
 * Icônes de capacités : trois états, pas deux.
 *
 * L'enjeu n'est pas de savoir dessiner une icône, c'est que **« on ne sait
 * pas » ne se lise jamais comme « non »**. Une icône est lue comme un fait ;
 * son absence aussi. Un fournisseur sans source doit donc afficher autre chose
 * que rien, sinon l'ignorance passe pour une constatation.
 *
 * `null` est falsy en JS : un test de vérité par troncature écraserait
 * exactement la distinction que le backend transporte. Ces tests existent pour
 * que ça devienne rouge.
 */
describe('ModuleBar — capacités des modèles', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  // `Tooltip` rend son contenu en TEXTE dans le DOM (un span masque par
  // `opacity`), pas en attribut `title` : les libelles se cherchent donc par
  // le texte. Ecrit d'abord en `getByTitle`, ces tests echouaient sur une UI
  // parfaitement correcte.
  const CAP = (o: boolean | null, v: boolean | null, r: boolean | null) =>
    ({ outils: o, vision: v, raisonnement: r })

  const ouvrirAvec = async (local: unknown[]) => {
    poserFetch({
      ...tableSaine(),
      '/models': { corps: { ...MODELES_OK, local } },
      '/models/loaded': { corps: { charges: [] } },
    })
    await rendre()
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    await waitFor(() => expect(screen.getByText('Local')).toBeTruthy())
  }

  it('une capacité déclarée présente montre son icône', async () => {
    await ouvrirAvec([{
      id: 'qwen3:8b', nom: 'qwen3:8b', provider: 'ollama', disponible: true,
      capacites: CAP(true, false, true),
    }])
    expect(screen.getByText('appels d’outils')).toBeTruthy()
    expect(screen.getByText('raisonnement')).toBeTruthy()
  })

  it('une capacité déclarée ABSENTE ne montre rien — l’absence est le fait', async () => {
    await ouvrirAvec([{
      id: 'qwen2.5:7b', nom: 'qwen2.5:7b', provider: 'ollama', disponible: true,
      capacites: CAP(true, false, false),
    }])
    await waitFor(() => expect(screen.getByText('appels d’outils')).toBeTruthy())
    expect(screen.queryByText('vision : traite les images')).toBeNull()
    expect(screen.queryByText('raisonnement')).toBeNull()
    // …et surtout PAS le marqueur d'inconnu : ici, on sait.
    expect(screen.queryByText('?')).toBeNull()
  })

  it('trois inconnues affichent un marqueur distinct, pas le silence', async () => {
    // Le cœur du sujet. Sans ce marqueur, un modèle LM Studio dont personne ne
    // connaît les capacités serait visuellement identique à un modèle Ollama
    // qui a déclaré n'en avoir aucune.
    await ouvrirAvec([{
      id: 'lmstudio:mystere', nom: 'mystere', provider: 'lmstudio', disponible: true,
      capacites: CAP(null, null, null),
    }])
    await waitFor(() => expect(screen.getByText('?')).toBeTruthy())
    expect(screen.queryByText('appels d’outils')).toBeNull()
  })

  it('`null` ne se lit pas comme `false` : aucune icône n’est déduite', async () => {
    await ouvrirAvec([{
      id: 'flm:qwen3vl-it:4b', nom: 'Qwen3 VL', provider: 'flm', disponible: true,
      capacites: CAP(null, true, null),
    }])
    // La vision est un fait (registre FLM tenu à la main) : elle s'affiche.
    await waitFor(() => expect(screen.getByText('vision : traite les images')).toBeTruthy())
    // Les deux autres sont inconnues : rien n'est affirmé, dans aucun sens.
    expect(screen.queryByText('appels d’outils')).toBeNull()
    expect(screen.queryByText('raisonnement')).toBeNull()
  })

  it('un modèle sans le champ (cloud) n’affiche ni icône ni marqueur', async () => {
    await ouvrirAvec([{
      id: 'qwen2.5:7b', nom: 'qwen2.5:7b', provider: 'ollama', disponible: true,
    }])
    await waitFor(() => expect(screen.getAllByText('qwen2.5:7b').length).toBeGreaterThan(0))
    expect(screen.queryByText('?')).toBeNull()
    expect(screen.queryByText('appels d’outils')).toBeNull()
  })
})

/**
 * Faisabilité matérielle (`GET /models/materiel`) — le résumé et les badges.
 *
 * Ce que ces tests gardent tient en deux points, et aucun des deux n'est
 * décoratif :
 *
 * **1. « On ne sait pas » ne doit jamais se lire comme « tout va bien ».** Le
 * verdict `inconnu` a son propre badge, exactement comme le « ? » des capacités
 * juste au-dessus. Un `verdict && <Badge/>` ou un `?? 'tient'` le ferait
 * disparaître — et c'est précisément l'état de TOUS les modèles LM Studio,
 * puisque ni `/v1/models` ni `/api/v0/models` ne publient de taille (mesuré sur
 * ce poste le 2026-09-06, pas supposé).
 *
 * **2. La mémoire dédiée et la mémoire partagée ne s'additionnent pas.** Sur
 * l'iGPU de ce poste, dxdiag rend `Dedicated: 512 MB` + `Shared: 16036 MB`, et
 * leur somme est ce qu'il appelle « Display Memory ». L'afficher annoncerait
 * ~16,2 Go de VRAM à côté de 31,3 Go de RAM, soit près de 48 Go à quelqu'un qui
 * en a 31 : les mêmes octets, comptés deux fois. Le test vérifie que les deux
 * nombres restent nommés séparément.
 *
 * **3. Le motif affiché doit être celui du calcul.** Depuis le 2026-09-06 les
 * verdicts se calculent sur la mémoire LIBRE, pas sur la totale : afficher le
 * total sous « verdicts calculés sur » annoncerait un motif qui ne correspond
 * plus au badge d'à côté. Un backend antérieur, lui, n'envoie pas ce champ —
 * la ligne doit alors dire qu'elle ne sait pas, jamais « 0 Go libres », qui se
 * lirait comme une machine saturée (§8, la discipline des frontières
 * `.json()`).
 *
 * Le corps rejoué est celui que `core/materiel.py` produit réellement sur ce
 * poste, `GET /models/materiel` à l'appui — pas une forme inventée.
 */
describe('ModuleBar — faisabilité matérielle', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  const GIO = 1024 ** 3
  const MO = 1024 * 1024

  /** L'état réel de ce poste : 31,3 Go de RAM, iGPU AMD, pas de FLM. */
  const MATERIEL_IGPU = {
    ram_octets: 33631817728,
    gpu: {
      nom: 'AMD Radeon(TM) 840M Graphics',
      vram_octets: 512 * MO,
      vram_partagee_octets: 16036 * MO,
      partage_la_ram: true,
      source: 'dxdiag',
    },
    npu: { disponible: false },
    ressource: { octets: 33631817728, origine: 'ram' },
    // Mesuré sur ce poste avec `qwen2.5:7b` résident : 9,47 Gio libres sur
    // 31,32 Gio. C'est l'écart que le calcul ignorait, et le seul chiffre qui
    // explique qu'un modèle de 13 Go « ne tienne pas » sur cette machine.
    ressource_libre: { octets: 10166943744, origine: 'ram' },
  }

  const ouvrirAvec = async (
    reponse: Reponse,
    local: unknown[] = MODELES_OK.local,
    charges: unknown[] = [],
  ) => {
    const fetchMock = poserFetch({
      ...tableSaine(),
      '/models': { corps: { ...MODELES_OK, local } },
      '/models/loaded': { corps: { charges } },
      '/models/load': { corps: { ok: true, message: '', charges: [] } },
      '/models/unload': { corps: { ok: true, message: '', charges: [] } },
      '/models/materiel': reponse,
    })
    await rendre()
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    await waitFor(() => expect(screen.getByText('Local')).toBeTruthy())
    return fetchMock
  }

  /** Combien de fois `/models/materiel` a été demandé. */
  const appelsMateriel = (m: ReturnType<typeof poserFetch>) =>
    m.mock.calls.filter(([u]) => String(u).includes('/models/materiel')).length

  it('annonce la RAM, le GPU et le NPU de la machine', async () => {
    await ouvrirAvec({ corps: { materiel: MATERIEL_IGPU, modeles: [] } })
    await waitFor(() => expect(screen.getByText('31,3 Go')).toBeTruthy())
    expect(screen.getByText(/AMD Radeon\(TM\) 840M Graphics/)).toBeTruthy()
    // NPU éteint : plus de badge muet, un bouton actionnable (cf. « Démarrer
    // FLM » ci-dessous).
    expect(screen.getByText('Démarrer FLM')).toBeTruthy()
  })

  it('garde la mémoire dédiée et la partagée SÉPARÉES — jamais leur somme', async () => {
    await ouvrirAvec({ corps: { materiel: MATERIEL_IGPU, modeles: [] } })
    const ligne = await screen.findByText(/AMD Radeon\(TM\) 840M Graphics/)
    // Les deux nombres, nommés, et les trois mots qui disent d'où vient le
    // second : sans eux, le lecteur additionne.
    expect(ligne.textContent).toContain('512 Mo dédiés')
    expect(ligne.textContent).toContain('partagés avec la RAM')
    // Et surtout PAS leur somme (« Display Memory » de dxdiag) : 512 + 16036
    // = 16548 Mo, soit 16,2 Go de mémoire qui n'existe pas séparément.
    expect(ligne.textContent).not.toContain('16,2 Go')
  })

  it('dit sur quelle ressource les verdicts sont calculés — la LIBRE, avec le total', async () => {
    // Sans cette ligne, « ne tient pas » est un jugement sans motif — et sur ce
    // poste le motif est contre-intuitif deux fois : c'est la RAM et non les
    // 16,2 Go de « mémoire d'affichage » que dxdiag annonce (celle-ci EST la
    // RAM), et c'est ce qui en RESTE et non ce que la machine contient.
    await ouvrirAvec({ corps: { materiel: MATERIEL_IGPU, modeles: [] } })
    await waitFor(() => expect(screen.getByText('verdicts calculés sur')).toBeTruthy())
    const ligne = screen.getByText(/libres sur/)
    expect(ligne.textContent).toContain('9,5 Go libres')
    // Le total reste affiché à côté : « 9,5 Go » seul ne dit pas si la machine
    // est petite ou simplement occupée.
    expect(ligne.textContent).toContain('sur 31,3 Go de RAM')
  })

  it('un backend sans `ressource_libre` ne fait pas afficher « 0 Go libres »', async () => {
    // Instance antérieure au 2026-09-06 : le champ n'existe pas. `materielDe`
    // normalise en « inconnu », et la ligne le dit — un zéro se lirait comme
    // une machine saturée, c'est-à-dire un diagnostic à la place d'une absence
    // de mesure. Même discipline que `materiel === null` plus haut.
    const { ressource_libre: _ignore, ...ancien } = MATERIEL_IGPU
    await ouvrirAvec({ corps: { materiel: ancien, modeles: [] } })
    await waitFor(() => expect(screen.getByText('verdicts calculés sur')).toBeTruthy())
    expect(screen.getByText('rien de mesurable')).toBeTruthy()
    expect(screen.queryByText(/0 o libres/)).toBeNull()
    expect(screen.queryByText(/0 Go libres/)).toBeNull()
    // Le reste du résumé, lui, tient toujours : la RAM totale est un autre
    // champ, et son absence n'est pas ce qu'on teste ici.
    expect(screen.getByText('31,3 Go')).toBeTruthy()
  })

  it('une lecture fraîche ratée se dit, elle ne se remplace pas par le total', async () => {
    // Le backend ne retombe JAMAIS sur la mémoire totale quand la sonde échoue
    // (ce serait rejouer le bug sous couvert de prudence) : il envoie
    // `origine: 'inconnu'`. La ligne doit refuser de l'habiller.
    const materiel = {
      ...MATERIEL_IGPU,
      ressource_libre: { octets: null, origine: 'inconnu' },
    }
    await ouvrirAvec({ corps: { materiel, modeles: [] } })
    await waitFor(() => expect(screen.getByText('verdicts calculés sur')).toBeTruthy())
    expect(screen.getByText('rien de mesurable')).toBeTruthy()
    expect(screen.queryByText(/31,3 Go de RAM/)).toBeNull()
  })

  it('relit le matériel après un chargement/déchargement — sinon les verdicts décrivent l’état d’AVANT', async () => {
    // Le denominateur des verdicts est desormais la memoire LIBRE : le bouton
    // « libérer » qui vit dans ce panneau change donc la reponse de
    // `/models/materiel`. Sans relecture, les badges d'a cote continueraient de
    // decrire l'etat d'avant l'action qu'on vient de declencher — et c'etait
    // correct par construction tant que le denominateur etait une constante
    // materielle, ce qui rend l'oubli d'autant plus facile.
    //
    // La relecture est DIFFEREE, et le delai est mesure : Ollama repond a un
    // dechargement en 0,01 s et `/api/ps` est deja vide, mais la memoire n'est
    // rendue qu'a +0,5 s. Relire tout de suite afficherait « 9,2 Go libres » a
    // cote d'un modele qu'on vient de liberer.
    const fetchMock = await ouvrirAvec(
      { corps: { materiel: MATERIEL_IGPU, modeles: [] } },
      [{ id: 'petit:1b', nom: 'petit:1b', provider: 'ollama', disponible: true }],
      // `/models/loaded` rend des OBJETS, pas des chaines : le composant lit
      // `m.id`. Une liste de chaines y donnerait des identifiants vides, donc
      // aucun modele « en memoire » et le bouton « charger » a la place.
      [{ id: 'petit:1b' }],
    )
    const avant = appelsMateriel(fetchMock)
    await act(async () => { screen.getByText('libérer').click() })
    await waitFor(() => expect(appelsMateriel(fetchMock)).toBeGreaterThan(avant),
                  { timeout: 3000 })
  })

  it('affiche un badge par verdict de mémoire', async () => {
    await ouvrirAvec(
      {
        corps: {
          materiel: MATERIEL_IGPU,
          modeles: [
            { id: 'petit:1b', provider: 'ollama', taille_octets: GIO, verdict: 'tient' },
            { id: 'moyen:20b', provider: 'ollama', taille_octets: 25 * GIO, verdict: 'limite' },
            { id: 'gros:70b', provider: 'ollama', taille_octets: 40 * GIO, verdict: 'ne_tiendra_pas' },
          ],
        },
      },
      [
        { id: 'petit:1b', nom: 'petit:1b', provider: 'ollama', disponible: true },
        { id: 'moyen:20b', nom: 'moyen:20b', provider: 'ollama', disponible: true },
        { id: 'gros:70b', nom: 'gros:70b', provider: 'ollama', disponible: true },
      ],
    )
    await waitFor(() => expect(screen.getByText('tient')).toBeTruthy())
    expect(screen.getByText('limite')).toBeTruthy()
    expect(screen.getByText('ne tient pas')).toBeTruthy()
  })

  it('« inconnu » a son propre badge — le silence se lirait comme « tout va bien »', async () => {
    // Le cas de TOUS les modèles LM Studio : aucune taille publiée, donc aucun
    // verdict possible. Sans badge, ils seraient visuellement identiques à des
    // modèles qui tiennent.
    await ouvrirAvec(
      {
        corps: {
          materiel: MATERIEL_IGPU,
          modeles: [{
            id: 'lmstudio:mystere', provider: 'lmstudio', taille_octets: null, verdict: 'inconnu',
          }],
        },
      },
      [{ id: 'lmstudio:mystere', nom: 'mystere', provider: 'lmstudio', disponible: true }],
    )
    await waitFor(() => expect(screen.getByText('taille ?')).toBeTruthy())
    expect(screen.queryByText('tient')).toBeNull()
  })

  it('un verdict que le frontend ne connaît pas retombe sur « inconnu », pas sur « tient »', async () => {
    // Backend plus récent, ou champ absent. Une valeur illisible est une
    // ignorance : elle doit prendre le mot qui le dit, jamais le bénéfice du
    // doute.
    await ouvrirAvec(
      {
        corps: {
          materiel: MATERIEL_IGPU,
          modeles: [
            { id: 'a:1b', provider: 'ollama', taille_octets: GIO, verdict: 'verdict_du_futur' },
            { id: 'b:1b', provider: 'ollama', taille_octets: GIO },
          ],
        },
      },
      [
        { id: 'a:1b', nom: 'a:1b', provider: 'ollama', disponible: true },
        { id: 'b:1b', nom: 'b:1b', provider: 'ollama', disponible: true },
      ],
    )
    await waitFor(() => expect(screen.getAllByText('taille ?').length).toBe(2))
    expect(screen.queryByText('tient')).toBeNull()
  })

  it('annonce le NPU quand FastFlowLM répond', async () => {
    await ouvrirAvec(
      {
        corps: {
          materiel: { ...MATERIEL_IGPU, npu: { disponible: true } },
          modeles: [{
            id: 'flm:qwen3:4b', provider: 'flm', taille_octets: null, verdict: 'disponible',
          }],
        },
      },
      [{ id: 'flm:qwen3:4b', nom: 'Qwen3 4B (NPU)', provider: 'flm', disponible: true }],
    )
    await waitFor(() => expect(screen.getByText('FastFlowLM répond')).toBeTruthy())
    expect(screen.getByText('NPU pret')).toBeTruthy()
  })

  /**
   * Démarrage de FLM depuis le bouton — la table statique de `ouvrirAvec` ne
   * suffit plus ici : la réponse de `/health` et `/models/materiel` doit
   * changer APRÈS le clic, pour rejouer le sondage réel du composant. Un
   * `fetch` maison, à la place de `poserFetch`, porte cette progression.
   */
  it('« Démarrer FLM » : lance le serveur, sonde /health, et se retire une fois joignable', async () => {
    let appelsDemarrage = 0
    let appelsHealth = 0
    const impl = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/models/flm/start')) {
        appelsDemarrage += 1
        return new Response(JSON.stringify({
          ok: true, raison: 'FLM démarre — patientez quelques secondes puis re-testez.',
        }), { status: 200 })
      }
      if (url.includes('/health')) {
        appelsHealth += 1
        // Joignable qu'à partir du DEUXIÈME sondage : la première sonde arrive
        // quasi tout de suite après le lancement, FLM n'a pas encore ouvert
        // son port — sans quoi ce test ne prouverait rien du sondage lui-même.
        return new Response(JSON.stringify({
          ollama: true, model: '', models: [], flm: appelsHealth >= 2, lmstudio: false,
        }), { status: 200 })
      }
      if (url.includes('/models/materiel')) {
        return new Response(JSON.stringify({
          materiel: { ...MATERIEL_IGPU, npu: { disponible: appelsHealth >= 2 } },
          modeles: [],
        }), { status: 200 })
      }
      const table: Record<string, Reponse> = { ...tableSaine(), '/models': { corps: MODELES_OK } }
      const cle = Object.keys(table).filter(k => url.includes(k)).sort((a, b) => b.length - a.length)[0]
      const { status = 200, corps } = cle ? table[cle] : { corps: ERREUR_500, status: 500 }
      return new Response(JSON.stringify(corps), {
        status, headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', impl)

    render(<ModuleBar module="docs" conversationId="" showFile showModel />)
    await act(async () => { await Promise.resolve() })
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    await waitFor(() => expect(screen.getByText('Démarrer FLM')).toBeTruthy())

    await act(async () => { screen.getByText('Démarrer FLM').click() })
    expect(appelsDemarrage).toBe(1)
    await waitFor(() => expect(screen.getByText('Démarrage…')).toBeTruthy())

    // Le sondage tourne réellement toutes les DELAI_SONDE_FLM_MS (1 s) — vraie
    // horloge, comme le test de relecture matérielle un peu plus haut.
    await waitFor(
      () => expect(screen.getByText('FastFlowLM répond')).toBeTruthy(),
      { timeout: 6000 },
    )
    expect(screen.queryByText('Démarrer FLM')).toBeNull()
    expect(screen.queryByText('Démarrage…')).toBeNull()
  }, 10000)

  it('double-clic sur « Démarrer FLM » ne déclenche qu’un seul appel réseau', async () => {
    // `/health` ne répond jamais joignable : le bouton doit rester bloqué en
    // « Démarrage… » pendant tout le test, pour isoler la seule question posée
    // ici — combien de fois `/models/flm/start` a été appelé.
    let appelsDemarrage = 0
    const impl = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/models/flm/start')) {
        appelsDemarrage += 1
        return new Response(JSON.stringify({ ok: true, raison: '' }), { status: 200 })
      }
      if (url.includes('/health')) {
        return new Response(JSON.stringify({
          ollama: true, model: '', models: [], flm: false, lmstudio: false,
        }), { status: 200 })
      }
      const table: Record<string, Reponse> = {
        ...tableSaine(),
        '/models': { corps: MODELES_OK },
        '/models/materiel': { corps: { materiel: MATERIEL_IGPU, modeles: [] } },
      }
      const cle = Object.keys(table).filter(k => url.includes(k)).sort((a, b) => b.length - a.length)[0]
      const { status = 200, corps } = cle ? table[cle] : { corps: ERREUR_500, status: 500 }
      return new Response(JSON.stringify(corps), {
        status, headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', impl)

    render(<ModuleBar module="docs" conversationId="" showFile showModel />)
    await act(async () => { await Promise.resolve() })
    await ouvrir('Modèle')
    await act(async () => { screen.getByText('Voir tous les modèles').click() })
    await waitFor(() => expect(screen.getByText('Démarrer FLM')).toBeTruthy())

    // Les deux clics partent dans le MÊME lot synchrone — c'est le double-clic
    // qu'un `useState` seul ne verrait pas (les deux liraient sa valeur d'avant
    // le premier rendu). `flmStartingRef` doit les départager.
    const bouton = screen.getByText('Démarrer FLM')
    await act(async () => {
      bouton.click()
      bouton.click()
    })
    expect(appelsDemarrage).toBe(1)
  })

  it('GPU indétectable : « inconnu », et surtout pas « aucun »', async () => {
    // Troisième état. Ni « pas de GPU » (qui autoriserait à conclure) ni « GPU
    // illimité » (qui autoriserait à promettre).
    await ouvrirAvec({
      corps: {
        materiel: {
          ...MATERIEL_IGPU,
          gpu: {
            nom: null, vram_octets: null, vram_partagee_octets: null,
            partage_la_ram: null, source: 'inconnu',
          },
        },
        modeles: [],
      },
    })
    await waitFor(() => expect(screen.getByText('inconnu')).toBeTruthy())
    expect(screen.queryByText(/Radeon/)).toBeNull()
  })

  it('une RAM à 0 se lit « inconnu », pas « machine sans mémoire »', async () => {
    // Un 0 rendu par le backend est une lecture ratée. « 0 Go » serait un
    // diagnostic, et il serait faux.
    await ouvrirAvec({ corps: { materiel: { ...MATERIEL_IGPU, ram_octets: 0 }, modeles: [] } })
    await waitFor(() => expect(screen.getByText('RAM')).toBeTruthy())
    expect(screen.queryByText('0 Mo')).toBeNull()
    expect(screen.queryByText('0,0 Go')).toBeNull()
  })

  it('la route absente (instance plus ancienne) ne produit AUCUN résumé, sans planter', async () => {
    await ouvrirAvec({ status: 404, corps: { detail: 'Not Found' } })
    await waitFor(() => expect(screen.getAllByText('qwen2.5:7b').length).toBeGreaterThan(0))
    expect(screen.queryByText('verdicts calculés sur')).toBeNull()
    expect(screen.queryByText('taille ?')).toBeNull()
  })

  it("le 500 du gestionnaire d'exceptions ne produit ni résumé ni badge", async () => {
    // `{"detail": …, "type": …}` n'a ni `materiel` ni `modeles` : le corps
    // parse parfaitement, donc aucun `.catch()` ne le voit. C'est LE piège du
    // §8, rejoué sur cette frontière-ci.
    await ouvrirAvec({ status: 500, corps: ERREUR_500 })
    await waitFor(() => expect(screen.getAllByText('qwen2.5:7b').length).toBeGreaterThan(0))
    expect(screen.queryByText('verdicts calculés sur')).toBeNull()
  })

  it('un corps 200 de forme inattendue ne casse rien non plus', async () => {
    // Un 200 ne garantit pas la forme : proxy, page d'erreur, version
    // intermédiaire. `materiel: null` et `modeles: "oups"` doivent traverser.
    await ouvrirAvec({ corps: { materiel: null, modeles: 'oups' } })
    await waitFor(() => expect(screen.getAllByText('qwen2.5:7b').length).toBeGreaterThan(0))
    expect(screen.queryByText('verdicts calculés sur')).toBeNull()
  })
})
