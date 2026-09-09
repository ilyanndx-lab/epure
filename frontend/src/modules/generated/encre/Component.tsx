import { useEffect, useRef, useState } from 'react'
import {
  CheckCircle2, Eraser, GraduationCap, PenLine, PenTool, Plus, Redo2, ScanText,
  SkipForward, Sigma, Trash2, Type, Undo2,
} from 'lucide-react'
import katex from 'katex'
import type { SharedModuleProps } from '../../registry'
import { API, apiFetch } from '../../../api'
import { dico, liste, texte } from '../../../normaliser'
import {
  COULEUR_ENCRE, dimensionner, HAUTEUR, LARGEUR, peindre, TAILLE_TRAIT, type Trait,
  useDessinStylet,
} from './useCanvasEncre'

/**
 * Module « encre » — prise de notes manuscrites au stylet, et leur transcription.
 *
 * Feuille de route complète dans `docs/module-encre.md`. La phase 1 (le canvas,
 * l'encre, zéro ML) garde sa valeur autonome ; la phase 2 y ajoute UN bouton et
 * une zone de lecture, et rien d'autre.
 *
 * ── Ce que la transcription est, et ce qu'elle n'est pas ─────────────────────
 *
 * **C'est un index, pas une sortie.** La décision §0 du document : l'encre reste
 * le document, le LaTeX transcrit sert à rendre les notes cherchables au milieu
 * des fiches. L'ExpRate mesuré en phase 0 est de 24 % — assez pour retrouver une
 * page, très loin de ce qu'il faudrait pour la relire. D'où trois absences
 * délibérées dans cette interface :
 *
 * 1. **le texte n'est pas éditable** (`readOnly`). La correction est la phase 3,
 *    et elle vient avec la collecte de paires (encre, LaTeX) qui la justifie ;
 * 2. **aucun rendu mathématique.** Afficher joliment un LaTeX faux à 76 % le
 *    ferait passer pour un résultat ; le texte brut dit ce qu'il est ;
 * 3. **le déclenchement est un BOUTON**, jamais automatique à l'enregistrement.
 *    La latence CPU réelle du modèle sur ce poste n'était pas mesurée quand la
 *    phase 2 a été décidée, donc un anti-rebond aurait été posé à l'aveugle.
 *
 * Aucun appel LLM n'existe sur ce chemin, dans aucune branche — le modèle de
 * transcription est un HMER dédié qui tourne en local, pas le modèle du chat.
 *
 * **Mode "maths" / "lettres" par page (2026-09-08).** `pix2text-mfr` est un
 * reconnaisseur de FORMULES mathématiques (sortie LaTeX), pas un OCR
 * généraliste — lui donner du texte manuscrit normal ne produit pas une
 * transcription dégradée mais des hallucinations de syntaxe math, un résultat
 * qui a l'air d'un LaTeX plausible sans en être un. Une page prise de notes en
 * mode "lettres" désactive donc le bouton « Transcrire ». **Le vrai refus est
 * côté serveur** (`POST /encre/pages/{id}/transcrire` répond 400 en mode
 * "lettres", cf. `modules/encre/router.py`) : le bouton désactivé n'est qu'un
 * confort, pas la garde — un appel direct à l'API contournerait un bouton
 * grisé sans jamais toucher un refus qui ne vivrait que côté client.
 *
 * ── Ce que ce fichier stocke, et pourquoi ça compte ──────────────────────────
 *
 * **Des vecteurs, jamais une image.** Un trait est une suite de points
 * `(x, y, pression, t)` ; le rendu bitmap n'existe que sur ce canvas, et il est
 * recalculé à chaque affichage. C'est la décision structurante du document —
 * « stocker l'encre brute, toujours » — et elle a une conséquence pratique
 * immédiate : le jour où la phase 2 branche un modèle de transcription, tout
 * l'historique déjà écrit sera retranscriptible. Un PNG ne le serait pas, et un
 * PNG ne se change pas non plus d'épaisseur ni d'échelle.
 *
 * **Une page a une taille LOGIQUE fixe** (`LARGEUR` × `HAUTEUR` ci-dessous), et
 * les coordonnées sont stockées dedans, pas en pixels d'écran. Sans ça, une note
 * écrite sur un écran large et rouverte sur une fenêtre étroite serait décalée
 * ou tronquée — le canvas, lui, est simplement mis à l'échelle pour tenir dans
 * la largeur disponible, et le `devicePixelRatio` n'affecte que la finesse du
 * rendu, jamais la donnée.
 *
 * ── Les trois pièges du stylet, tous payés par d'autres avant nous ───────────
 *
 * 1. **`getCoalescedEvents()`**. Un stylet actif échantillonne à 240 Hz ; le
 *    navigateur ne délivre qu'un `pointermove` par trame (60 Hz) et REGROUPE les
 *    autres dedans. Ne lire que l'événement lui-même perd les trois quarts des
 *    points, et ça se voit : les courbes rapides deviennent des segments.
 * 2. **`touch-action: none`**. Sans lui, le navigateur interprète le geste comme
 *    un défilement et n'envoie plus rien après quelques pixels.
 * 3. **Le rejet de la paume**. La main repose sur l'écran pendant qu'on écrit,
 *    et elle arrive souvent AVANT la pointe du stylet. Deux règles, pas une
 *    seule : tant qu'un stylet est en contact, tout `pointerType === 'touch'`
 *    est ignoré ; et quand un stylet se pose alors qu'un trait tactile a déjà
 *    commencé, ce trait est ABANDONNÉ. Sans la seconde, la première arrive trop
 *    tard — la paume a déjà tracé sa trace.
 *
 * ── Pourquoi aucune réponse n'est crue sur sa forme ──────────────────────────
 *
 * Chaque `.json()` passe par `liste()`/`dico()`/`texte()` (`src/normaliser.ts`).
 * `r.json() as {pages: …}` est une AFFIRMATION que TypeScript croit sur parole,
 * et une application locale répond régulièrement autre chose : 401 avant
 * appairage, 404 sur une instance qui n'a pas le module, 500 du gestionnaire
 * d'exceptions. Le champ annoncé vaut alors `undefined` et la faute n'apparaît
 * qu'au rendu suivant, sur un `.length`. Éprouvé dans `Component.test.tsx`.
 *
 * De la même famille, un cran plus tôt : **`apiFetch` ne lève JAMAIS sur un
 * statut HTTP**. Un `try { await apiFetch(…); marquerEnregistré() } catch {}` ne
 * rattrape que les pannes réseau — sur un 404, la promesse résout et le code de
 * succès s'exécute. Toute mise à jour d'état qui SIGNIFIE « enregistré » est
 * donc gardée par `res.ok`, et le message du backend est affiché.
 */

//: Délai d'inactivité avant l'enregistrement automatique. Assez court pour
//: qu'une pause entre deux mots suffise, assez long pour ne pas envoyer une
//: requête par trait.
const DELAI_ENREGISTREMENT_MS = 1200

interface PageResume {
  id: string
  titre: string
  date_creation: string
  date_modification: string
  n_traits: number
}

/**
 * Ce que le backend a écrit dans `page.transcription` (cf. `EncreEngine`).
 *
 * `modele` et `version` sont AFFICHÉS et pas seulement stockés : ils disent sur
 * quel pipeline ce texte a été produit, ce qui est la seule information capable
 * d'expliquer pourquoi deux pages transcrites à trois mois d'écart ne se
 * ressemblent pas. `docs/module-encre.md` les demande pour pouvoir retranscrire
 * l'historique quand le modèle change.
 */
interface Transcription {
  texte: string
  modele: string
  version: string
  date: string
}

/** Résultat d'un appel d'écriture, tel qu'affiché dans le bandeau d'état. */
interface Etat {
  texte: string
  erreur: boolean
}

/**
 * Les deux seules valeurs connues côté client, miroir de `_MODES_VALIDES`
 * (`core/encre.py`). Une page en mode "lettres" n'est pas transcriptible —
 * `pix2text-mfr` reconnaît des formules, pas du texte manuscrit courant.
 */
type Mode = 'maths' | 'lettres'

/**
 * Ce que le serveur a confirmé avoir enregistré. `null` = aucune page chargée.
 *
 * C'est un instantané par RÉFÉRENCE, pas une copie : `traits` y est le tableau
 * exact qui a été envoyé. La comparaison « y a-t-il quelque chose à
 * enregistrer ? » est donc une égalité de références, et elle est juste parce
 * que toute modification de l'encre passe par `appliquer()`, qui construit un
 * NOUVEAU tableau. Voir `sale` plus bas pour ce que ça évite.
 *
 * `mode` y est comparé par VALEUR et non par référence — contrairement à
 * `traits` — parce que c'est une chaîne : deux chaînes égales sont toujours le
 * même mode, il n'y a pas d'équivalent au piège de `traits` ici.
 */
interface Instantane {
  titre: string
  traits: Trait[]
  mode: Mode
}

/**
 * Une entrée de `GET /encre/entrainement/a_corriger` (phase 3). Pas de tracés
 * ici — cf. `EncreEngine.list_pages_transcrites` côté backend : la correction
 * ne modifie que le TEXTE, jamais l'encre, donc rien ne les affiche.
 */
interface PageACorreger {
  id: string
  titre: string
  texte_modele: string
  /** `null` = jamais validée. Présent, c'est un horodatage — sa VALEUR ne
   * sert à rien ici, seule sa présence compte (filtrage par défaut). */
  validee_le: string | null
}

/** Une expression à recopier (`GET /encre/entrainement/dictee/expression`). */
interface ExpressionDictee {
  id: string
  latex: string
}

// ── Normalisation des réponses ───────────────────────────────────────────────

/**
 * `dico()` rend un `Record<string, boolean>` — son usage d'origine est une table
 * de drapeaux. Le rétrécissement de type ne nous convient pas ici, mais le
 * CONTRÔLE, si : c'est lui qui écarte `null`, un tableau et une chaîne, sur
 * lesquels un `'champ' in v` lèverait. Un `Record<string, boolean>` est
 * assignable à un `Record<string, unknown>` sans conversion, donc on garde le
 * garde-fou partagé plutôt que d'en réécrire un ici — c'est précisément la
 * duplication que `src/normaliser.ts` existe pour supprimer.
 */
function objet(v: unknown): Record<string, unknown> {
  return dico(v)
}

function versEntier(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

/**
 * Une entrée de liste, quoi qu'ait répondu le backend.
 *
 * Une entrée sans `id` est écartée et non « réparée » : elle serait affichée,
 * cliquable, et mènerait à un 404 que l'utilisateur ne saurait pas lire.
 */
function versResumes(v: unknown): PageResume[] {
  return liste<unknown>(v)
    .map(brut => {
      const o = objet(brut)
      return {
        id: texte(o.id),
        titre: texte(o.titre),
        date_creation: texte(o.date_creation),
        date_modification: texte(o.date_modification),
        n_traits: versEntier(o.n_traits),
      }
    })
    .filter(p => p.id !== '')
}

/**
 * La transcription d'une page, ou `null`.
 *
 * `null` et non un objet à champs vides : « cette page n'a jamais été
 * transcrite » et « elle l'a été et le modèle n'a rien lu » sont deux états
 * différents, et le second doit s'afficher — c'est lui qui dit à l'utilisateur
 * que le bouton a bien fonctionné et que le modèle n'a rien reconnu.
 *
 * Une transcription sans `date` est écartée : le backend en écrit toujours une,
 * donc son absence signifie qu'on lit autre chose que le champ attendu (un corps
 * d'erreur, une réponse d'une autre instance). Cf. `src/normaliser.ts`.
 */
function versTranscription(v: unknown): Transcription | null {
  const o = objet(v)
  const date = texte(o.date)
  if (!date) return null
  return {
    texte: texte(o.texte),
    modele: texte(o.modele),
    version: texte(o.version),
    date,
  }
}

/**
 * Les traits d'une page, normalisés point par point.
 *
 * C'est le seul endroit du composant qui regarde À L'INTÉRIEUR d'un trait, et
 * c'est volontairement plus strict que le backend (qui les traite comme opaques,
 * cf. `core/encre.py`). La raison est asymétrique : le serveur doit pouvoir
 * stocker un champ qu'il ne connaît pas — c'est ce qui rendra la phase 2
 * possible sans migration — alors que le rendu, lui, va passer ces nombres à
 * `getStroke` et à `Path2D`. Un `undefined` y produirait un `NaN`, c'est-à-dire
 * une page BLANCHE dont le fichier est pourtant intact : la pire forme d'échec,
 * celle qui ressemble à une perte de données sans en être une.
 *
 * Un trait sans point est écarté : il ne se dessine pas, et il compterait quand
 * même dans la pile d'annulation.
 */
function versTraits(v: unknown): Trait[] {
  return liste<unknown>(v)
    .map(brut => {
      const o = objet(brut)
      return {
        couleur: texte(o.couleur) || COULEUR_ENCRE,
        taille: versEntier(o.taille) || TAILLE_TRAIT,
        points: liste<unknown>(o.points).map(p => {
          const pt = objet(p)
          return {
            x: versEntier(pt.x),
            y: versEntier(pt.y),
            pression: versEntier(pt.pression) || 0.5,
            t: versEntier(pt.t),
          }
        }),
      }
    })
    .filter(t => t.points.length > 0)
}

/**
 * Le mode d'une page, quoi qu'ait répondu le backend.
 *
 * Toute valeur qui n'est pas exactement `"lettres"` — absente, mal formée, un
 * corps d'erreur qui n'a pas ce champ, une page écrite avant que ce champ
 * existe — se lit comme `"maths"`. C'est le MÊME défaut que
 * `EncreEngine._mode` côté serveur (`core/encre.py`) : une page ancienne doit
 * se comporter à l'identique des deux côtés, sans qu'aucun des deux ait besoin
 * de migrer quoi que ce soit.
 */
function versMode(v: unknown): Mode {
  return texte(v) === 'lettres' ? 'lettres' : 'maths'
}

/**
 * Les pages « à corriger » (phase 3), quoi qu'ait répondu le backend.
 *
 * Une entrée sans `id` est écartée, même règle que `versResumes` : affichée et
 * cliquable, elle mènerait à une validation sur un identifiant vide.
 */
function versPagesACorreger(v: unknown): PageACorreger[] {
  return liste<unknown>(v)
    .map(brut => {
      const o = objet(brut)
      const transcription = objet(o.transcription)
      const validee = texte(transcription.validee_le)
      return {
        id: texte(o.id),
        titre: texte(o.titre),
        texte_modele: texte(transcription.texte),
        validee_le: validee || null,
      }
    })
    .filter(p => p.id !== '')
}

/**
 * Message d'erreur du backend, ou un repli.
 *
 * `detail` est lu en vérifiant son TYPE : une erreur de validation FastAPI y met
 * une LISTE d'objets, et l'afficher brute donnerait « [object Object] ».
 */
async function messageDErreur(res: Response, repli: string): Promise<string> {
  try {
    const detail = objet(await res.json()).detail
    return typeof detail === 'string' && detail ? detail : `${repli} (HTTP ${res.status})`
  } catch {
    return `${repli} (HTTP ${res.status})`
  }
}

// ── Réseau ───────────────────────────────────────────────────────────────────

/**
 * Enregistre une page. Définie au NIVEAU MODULE et non dans le composant.
 *
 * Ce n'est pas un choix de style : l'effet d'enregistrement automatique
 * l'appelle, et une fonction déclarée dans le corps du composant devrait alors
 * figurer dans son tableau de dépendances — où elle changerait à chaque rendu.
 * Hors du composant, elle est stable par construction, et le bouton
 * « Enregistrer » appelle exactement le même code que la sauvegarde
 * automatique : deux chemins d'écriture qui divergeraient finiraient par ne pas
 * enregistrer la même chose.
 */
async function sauvegarder(
  pageId: string, titre: string, traits: Trait[], mode: Mode,
): Promise<Etat> {
  try {
    const res = await apiFetch(`${API}/encre/pages/${encodeURIComponent(pageId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ titre, strokes: traits, mode }),
    })
    // `res.ok` AVANT toute mise à jour d'état qui signifie un succès : `apiFetch`
    // résout sur un 404 comme sur un 200.
    if (!res.ok) {
      return { texte: await messageDErreur(res, 'Enregistrement refusé'), erreur: true }
    }
    const heure = new Date().toLocaleTimeString('fr-FR')
    return { texte: `Enregistré à ${heure}`, erreur: false }
  } catch {
    return { texte: 'Enregistrement impossible : backend injoignable.', erreur: true }
  }
}

// ── Rendu LaTeX (phase 3) ────────────────────────────────────────────────────

/**
 * Rendu KaTeX d'une expression, jamais une exception. `katex` est déjà une
 * dépendance du dépôt (`RichMessage.tsx`, via `rehype-katex`) — appelé ici
 * directement (`renderToString`) puisqu'on rend une expression ISOLÉE, pas un
 * document markdown complet. `throwOnError: false` rend un message d'erreur
 * KaTeX inline plutôt que de lever : une expression mal formée (LaTeX vérité en
 * cours de frappe, sortie du modèle syntaxiquement fausse) ne doit pas faire
 * planter l'écran de correction ou de dictée.
 */
function rendreLatex(tex: string): string {
  try {
    return katex.renderToString(tex, {
      throwOnError: false, strict: false, trust: false,
      macros: { '\\R': '\\mathbb{R}', '\\N': '\\mathbb{N}', '\\Z': '\\mathbb{Z}' },
    })
  } catch {
    return ''
  }
}

function RenduLatex({ tex }: { tex: string }) {
  if (!tex.trim()) {
    return <p className="text-xs text-muted italic">Rien à afficher.</p>
  }
  // dangerouslySetInnerHTML : sortie de KaTeX (`rendreLatex`), jamais du HTML utilisateur brut.
  return <div className="text-sm text-primary overflow-x-auto" dangerouslySetInnerHTML={{ __html: rendreLatex(tex) }} />
}

// ── Composant ────────────────────────────────────────────────────────────────

export default function EncreModule(_props: SharedModuleProps) {
  const [pages, setPages] = useState<PageResume[]>([])
  const [pageId, setPageId] = useState('')
  const [titre, setTitre] = useState('')
  //: Toute la mécanique du canvas (traits, outil, undo/redo, rendu) — extraite
  //: dans `useCanvasEncre.ts` (phase 3) pour être partagée avec le canvas de
  //: dictée inversée plus bas. Cf. son en-tête pour les trois pièges du stylet
  //: qu'elle tient à elle seule.
  //
  // Déstructuré ICI plutôt que gardé comme un seul objet `canvas.xxx` : un
  // outil de lint du dépôt (`react-hooks/refs`) signale toute lecture PAR
  // PROPRIÉTÉ d'un objet renvoyé par un hook et contenant une ref quelque
  // part, y compris sur des champs qui n'en sont pas (`onPointerDown`, une
  // simple fonction) — mesuré sur ce fichier avant ce commit. La
  // déstructuration donne des liaisons locales que l'outil suit correctement,
  // sans changer le comportement runtime.
  const {
    traits, outil, setOutil, peutAnnuler, peutRefaire, annuler, refaire, charger,
    canvasRef, zoneRef, onPointerDown, onPointerMove, terminer,
  } = useDessinStylet()
  //: Mode de la page courante. "maths" par défaut : c'est celui de toute page
  //: créée avant ce champ, et le mode que ce module a toujours transcrit.
  const [mode, setMode] = useState<Mode>('maths')
  //: Dernier état CONFIRMÉ par le serveur. Cf. `Instantane` et `sale`.
  const [enregistre, setEnregistre] = useState<Instantane | null>(null)
  const [etat, setEtat] = useState<Etat | null>(null)
  //: Transcription de la page courante, telle que le serveur l'a écrite. `null`
  //: = jamais transcrite. Jamais dérivée d'autre chose : le texte affiché est
  //: celui qui est SUR LE DISQUE et dans l'index, pas un résultat gardé en
  //: mémoire — sinon un rechargement montrerait autre chose que le bouton.
  const [transcription, setTranscription] = useState<Transcription | null>(null)
  //: Un appel de transcription est-il en cours ? Le modèle tourne sur le CPU et
  //: son premier appel charge 118 Mo de poids : sans cet état, l'interface reste
  //: muette pendant des dizaines de secondes et l'utilisateur reclique.
  const [transcrit, setTranscrit] = useState(false)
  //: Incrémenté pour redemander la liste. Un compteur plutôt qu'une fonction de
  //: rechargement : une fonction devrait figurer dans les dépendances de l'effet
  //: qui charge, et y changerait à chaque rendu.
  const [rafraichir, setRafraichir] = useState(0)

  /**
   * Y a-t-il quelque chose à enregistrer ? **Dérivé, jamais stocké.**
   *
   * C'était un drapeau booléen, et il perdait de l'encre. Le scénario, qui n'a
   * rien d'exotique : l'enregistrement automatique part avec les traits A ;
   * l'utilisateur continue d'écrire pendant la requête, les traits deviennent B ;
   * la réponse arrive et baisse le drapeau. B n'est alors plus « à enregistrer »
   * alors que le serveur n'a jamais vu que A — les derniers traits sont perdus
   * au prochain rechargement, sans un message, et sans que rien n'ait échoué.
   *
   * Comparer à ce que le serveur a RÉELLEMENT confirmé supprime le cas par
   * construction : confirmer A alors que l'état courant est B laisse la page
   * différente de son instantané, donc l'effet reprogramme un envoi. Aucun
   * drapeau ne peut plus mentir, parce qu'il n'y a plus de drapeau.
   *
   * Bénéfice inattendu et correct : annuler jusqu'à revenir exactement à l'état
   * enregistré rend la page propre — le tableau redevient le MÊME objet — au
   * lieu de réenregistrer un contenu identique.
   */
  const sale = enregistre !== null
    && (enregistre.titre !== titre || enregistre.traits !== traits || enregistre.mode !== mode)

  // ── Chargement de la liste ────────────────────────────────────────────────
  useEffect(() => {
    let vivant = true
    apiFetch(`${API}/encre/pages`)
      .then(r => r.json())
      .then(d => { if (vivant) setPages(versResumes(objet(d).pages)) })
      // Aucun `setPages(undefined)` possible : `versResumes` rend toujours un
      // tableau, y compris sur un corps d'erreur. Le `catch` ne couvre que la
      // panne réseau et le corps non-JSON.
      .catch(() => { if (vivant) setPages([]) })
    return () => { vivant = false }
  }, [rafraichir])

  // ── Chargement de la page courante ────────────────────────────────────────
  useEffect(() => {
    // Sortie SANS `setState`. La remise à zéro de la page courante est faite par
    // `reinitialiser()` dans les gestionnaires qui changent de page, et pas ici :
    // un `setState` synchrone dans le corps d'un effet déclenche un rendu en
    // cascade, ce qu'eslint refuse (`react-hooks/set-state-in-effect`). Ce n'est
    // pas qu'une contrainte d'outil — c'est aussi ce qui garantit que le vidage
    // et le changement de `pageId` partent dans le MÊME lot (cf. `ouvrirPage`).
    if (!pageId) return
    let vivant = true
    apiFetch(`${API}/encre/pages/${encodeURIComponent(pageId)}`)
      .then(async r => {
        if (!r.ok) throw new Error(await messageDErreur(r, 'Page illisible'))
        return r.json()
      })
      .then(d => {
        if (!vivant) return
        const page = objet(d)
        const titreCharge = texte(page.titre)
        const traitsCharges = versTraits(page.strokes)
        const modeCharge = versMode(page.mode)
        setTitre(titreCharge)
        charger(traitsCharges)
        setMode(modeCharge)
        // LES MÊMES objets que ceux posés dans l'état : l'instantané se compare
        // par référence, en poser une copie rendrait la page sale d'emblée et
        // déclencherait un enregistrement inutile à chaque ouverture.
        setEnregistre({ titre: titreCharge, traits: traitsCharges, mode: modeCharge })
        setTranscription(versTranscription(page.transcription))
        setEtat(null)
      })
      .catch((err: unknown) => {
        if (!vivant) return
        // On QUITTE la page au lieu d'afficher un canvas vide à sa place : une
        // page blanche que l'enregistrement automatique recopierait ensuite
        // par-dessus le fichier réel effacerait l'encre. L'instantané est remis
        // à `null` pour la même raison : c'est lui qui autorise une écriture, et
        // il n'y a rien à écrire de ce qu'on n'a pas su lire.
        setEnregistre(null)
        setPageId('')
        setEtat({ texte: err instanceof Error ? err.message : 'Page illisible', erreur: true })
      })
    return () => { vivant = false }
    // `charger` est stable (`useCallback([])` dans `useCanvasEncre.ts`) :
    // l'ajouter aux dépendances satisfait `react-hooks/exhaustive-deps` sans
    // faire rejouer cet effet à chaque trait dessiné.
  }, [pageId, charger])

  // ── Enregistrement automatique ────────────────────────────────────────────
  useEffect(() => {
    if (!sale || !pageId) return
    const minuteur = setTimeout(() => {
      // Capturé AVANT l'envoi : c'est exactement ce que le serveur va recevoir,
      // donc exactement ce qu'on aura le droit de dire « enregistré ».
      const envoye: Instantane = { titre, traits, mode }
      void sauvegarder(pageId, envoye.titre, envoye.traits, envoye.mode).then(resultat => {
        setEtat(resultat)
        // L'instantané n'avance QUE sur un succès confirmé : sur un refus, la
        // page reste sale et l'effet reprogrammera un envoi. Un enregistrement
        // automatique qui abandonne laisse le contenu dans le seul navigateur.
        if (!resultat.erreur) {
          setEnregistre(envoye)
          setRafraichir(n => n + 1)
        }
      })
    }, DELAI_ENREGISTREMENT_MS)
    return () => clearTimeout(minuteur)
  }, [sale, pageId, titre, traits, mode])

  // ── Avertissement avant fermeture ─────────────────────────────────────────
  useEffect(() => {
    if (!sale) return
    // Second filet : contrairement au module Code, l'encre n'est PAS dans
    // `localStorage`. Une page fermée avec un enregistrement en attente est
    // perdue pour de bon, d'où la confirmation.
    const avertir = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', avertir)
    return () => window.removeEventListener('beforeunload', avertir)
  }, [sale])

  // ── Changement de page ────────────────────────────────────────────────────

  /** Vide l'état de la page courante. Ne touche pas à `pageId`. */
  const reinitialiser = () => {
    setEnregistre(null)
    charger([])
    setTitre('')
    setMode('maths')
    setTranscription(null)
    setEtat(null)
  }

  /**
   * Ouvre une page — le SEUL chemin autorisé pour changer de page.
   *
   * ⚠️ Ce qu'il empêche vaut d'être écrit, parce que la faute est invisible et
   * destructrice : l'effet d'enregistrement lit `pageId` ET `traits`. Changer
   * `pageId` sans vider `traits` dans le même lot ferait donc écrire l'encre de
   * la page qu'on QUITTE dans le fichier de celle qu'on OUVRE — la seconde page
   * serait écrasée par la première, sans un message, et sans que la première
   * ait rien perdu qui mette la puce à l'oreille.
   *
   * Ce qui reste en attente est enregistré au passage, avec les valeurs
   * CAPTURÉES avant le vidage. Fait après, ça sauvegarderait une page vide.
   */
  const ouvrirPage = (id: string) => {
    if (id === pageId) return
    const enAttente = sale && pageId ? { pageId, titre, traits, mode } : null
    reinitialiser()
    setPageId(id)
    if (enAttente) {
      void sauvegarder(enAttente.pageId, enAttente.titre, enAttente.traits, enAttente.mode)
        .then(resultat => { if (resultat.erreur) setEtat(resultat) })
    }
  }

  // ── Actions sur les pages ─────────────────────────────────────────────────

  const nouvellePage = async () => {
    try {
      const res = await apiFetch(`${API}/encre/pages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strokes: [] }),
      })
      if (!res.ok) {
        setEtat({ texte: await messageDErreur(res, 'Création refusée'), erreur: true })
        return
      }
      const id = texte(objet(await res.json()).id)
      if (!id) {
        setEtat({ texte: 'Le serveur a créé une page sans identifiant.', erreur: true })
        return
      }
      ouvrirPage(id)
      setRafraichir(n => n + 1)
    } catch {
      setEtat({ texte: 'Création impossible : backend injoignable.', erreur: true })
    }
  }

  const supprimerPage = async (id: string) => {
    try {
      const res = await apiFetch(`${API}/encre/pages/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      })
      if (!res.ok) {
        setEtat({ texte: await messageDErreur(res, 'Suppression refusée'), erreur: true })
        return
      }
      if (id === pageId) {
        // `reinitialiser()` et non `ouvrirPage('')` : celui-ci enregistrerait ce
        // qui est en attente, c'est-à-dire réécrirait la page qu'on vient de
        // supprimer. Le PUT échouerait en 404 et afficherait une erreur pour une
        // opération qui, elle, a réussi.
        reinitialiser()
        setPageId('')
      }
      setRafraichir(n => n + 1)
    } catch {
      setEtat({ texte: 'Suppression impossible : backend injoignable.', erreur: true })
    }
  }

  const enregistrerMaintenant = async () => {
    // `enregistre` et pas seulement `pageId` : entre la sélection d'une page et
    // l'arrivée de son contenu, l'état local est VIDE. Enregistrer dans cette
    // fenêtre écrirait une page blanche par-dessus de l'encre réelle — c'est
    // court, mais c'est un clic, et le résultat est irrécupérable.
    if (!pageId || !enregistre) return
    const envoye: Instantane = { titre, traits, mode }
    const resultat = await sauvegarder(pageId, envoye.titre, envoye.traits, envoye.mode)
    setEtat(resultat)
    if (!resultat.erreur) {
      setEnregistre(envoye)
      setRafraichir(n => n + 1)
    }
  }

  /**
   * Transcrit la page courante. **Enregistre d'abord ce qui est en attente.**
   *
   * Ce premier point est le seul qui ne se déduit pas, et l'omettre donnerait un
   * bug silencieux et déroutant : le backend transcrit la page TELLE QU'ELLE EST
   * SUR LE DISQUE. Sans enregistrement préalable, écrire une formule puis
   * cliquer aussitôt transcrirait l'état d'avant — au mieux la formule
   * précédente, au pire une page vide — et rien dans l'interface ne dirait
   * pourquoi. L'enregistrement automatique attend 1,2 s d'inactivité, donc la
   * fenêtre n'a rien de théorique : c'est le geste normal.
   *
   * Si l'enregistrement échoue, on n'appelle PAS la transcription : mieux vaut
   * un message d'erreur que transcrire un contenu que l'utilisateur ne voit plus.
   *
   * `mode === 'lettres'` coupe court AVANT tout appel réseau — même garde que
   * le bouton désactivé. Ce n'est qu'un confort : le vrai refus, celui qui
   * protège un appel direct à l'API, est le 400 posé par le serveur (cf.
   * l'en-tête de ce fichier).
   *
   * `res.ok` avant toute mise à jour d'état qui signifie un succès : `apiFetch`
   * résout sur un 500 comme sur un 200, et un 500 est ici un cas prévu — pile de
   * transcription absente, poids introuvables. Un `catch` seul ne le verrait pas.
   */
  const transcrire = async () => {
    if (!pageId || !enregistre || transcrit || mode === 'lettres') return
    setTranscrit(true)
    try {
      if (sale) {
        const envoye: Instantane = { titre, traits, mode }
        const enregistrement = await sauvegarder(pageId, envoye.titre, envoye.traits, envoye.mode)
        if (enregistrement.erreur) { setEtat(enregistrement); return }
        setEnregistre(envoye)
      }
      setEtat({ texte: 'Transcription en cours…', erreur: false })
      const res = await apiFetch(
        `${API}/encre/pages/${encodeURIComponent(pageId)}/transcrire`,
        { method: 'POST' })
      if (!res.ok) {
        setEtat({ texte: await messageDErreur(res, 'Transcription refusée'), erreur: true })
        return
      }
      const page = objet(await res.json())
      const lue = versTranscription(page.transcription)
      setTranscription(lue)
      setEtat(lue && lue.texte
        ? { texte: 'Transcription terminée.', erreur: false }
        // Un texte vide est un RÉSULTAT, pas une panne : le modèle n'a rien
        // reconnu. Le dire explicitement, sinon le bouton a l'air d'être resté
        // sans effet.
        : { texte: "Transcription terminée : le modèle n'a rien reconnu.", erreur: false })
      setRafraichir(n => n + 1)
    } catch {
      setEtat({ texte: 'Transcription impossible : backend injoignable.', erreur: true })
    } finally {
      setTranscrit(false)
    }
  }

  // ── Phase 3 : correction ──────────────────────────────────────────────────

  const [vue, setVue] = useState<'notes' | 'entrainement'>('notes')
  const [sousVue, setSousVue] = useState<'correction' | 'dictee'>('correction')

  const [pagesACorreger, setPagesACorreger] = useState<PageACorreger[]>([])
  //: Cache les pages déjà validées par défaut — « ne pas re-proposer » sans
  //: jamais bloquer un retour dessus, cf. `docs/module-encre.md` (phase 3) :
  //: c'est un choix d'AFFICHAGE, le backend continue de les rendre.
  const [afficherValidees, setAfficherValidees] = useState(false)
  const [rafraichirCorrection, setRafraichirCorrection] = useState(0)
  const [pageCorrectionId, setPageCorrectionId] = useState('')
  const [texteCorrection, setTexteCorrection] = useState('')
  const [etatCorrection, setEtatCorrection] = useState<Etat | null>(null)
  const [validationEnCours, setValidationEnCours] = useState(false)
  //: Tracés de la page en correction, pour l'aperçu en lecture seule sous le
  //: texte édité. `GET /encre/entrainement/a_corriger` (ci-dessous) ne les
  //: porte PAS — cf. `PageACorreger` : la correction ne touche jamais l'encre,
  //: donc cette liste s'en passe. Redemandés via le MÊME endpoint que la vue
  //: Notes (`GET /encre/pages/{id}`), qui les rend.
  const [traitsCorrection, setTraitsCorrection] = useState<Trait[]>([])
  const canvasRefCorrection = useRef<HTMLCanvasElement | null>(null)
  const zoneRefCorrection = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (vue !== 'entrainement' || sousVue !== 'correction') return
    let vivant = true
    apiFetch(`${API}/encre/entrainement/a_corriger`)
      .then(r => r.json())
      .then(d => { if (vivant) setPagesACorreger(versPagesACorreger(objet(d).pages)) })
      .catch(() => { if (vivant) setPagesACorreger([]) })
    return () => { vivant = false }
  }, [vue, sousVue, rafraichirCorrection])

  useEffect(() => {
    // Sortie SANS `setState`, même idiome que le chargement de la page de
    // notes plus haut : `traitsCorrection` vaut déjà `[]` à l'initialisation,
    // et `choisirPageCorrection` (hors effet) le vide explicitement à chaque
    // changement de page — un `setState` synchrone ici déclencherait un rendu
    // en cascade qu'eslint refuse (`react-hooks/set-state-in-effect`).
    if (!pageCorrectionId) return
    let vivant = true
    apiFetch(`${API}/encre/pages/${encodeURIComponent(pageCorrectionId)}`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (vivant) setTraitsCorrection(d ? versTraits(objet(d).strokes) : []) })
      .catch(() => { if (vivant) setTraitsCorrection([]) })
    return () => { vivant = false }
  }, [pageCorrectionId])

  /**
   * Peint l'aperçu en lecture seule — `dimensionner`/`peindre` de
   * `useCanvasEncre.ts` directement, SANS passer par `useDessinStylet` : ce
   * canvas n'a ni gomme, ni undo/redo, ni gestionnaire de pointeur à porter,
   * juste l'encre déjà écrite à afficher telle quelle.
   */
  useEffect(() => {
    const canvas = canvasRefCorrection.current
    const zone = zoneRefCorrection.current
    if (!canvas || !zone) return
    if (dimensionner(canvas, zone, LARGEUR, HAUTEUR)) {
      peindre(canvas, LARGEUR, HAUTEUR, traitsCorrection, null)
    }
    if (typeof ResizeObserver === 'undefined') return
    const observateur = new ResizeObserver(() => {
      if (dimensionner(canvas, zone, LARGEUR, HAUTEUR)) {
        peindre(canvas, LARGEUR, HAUTEUR, traitsCorrection, null)
      }
    })
    observateur.observe(zone)
    return () => observateur.disconnect()
  }, [traitsCorrection])

  const choisirPageCorrection = (p: PageACorreger) => {
    setPageCorrectionId(p.id)
    setTexteCorrection(p.texte_modele)
    setEtatCorrection(null)
    // Vidé ICI, dans le gestionnaire, et pas dans l'effet ci-dessus : sinon
    // l'aperçu de la page qu'on QUITTE resterait affiché sous le texte de
    // celle qu'on OUVRE le temps que la requête réponde.
    setTraitsCorrection([])
  }

  /**
   * Valide la transcription courante. `telQuel` omet `texte` du corps :
   * `EncreEngine`/le routeur reprennent alors `texte_modele` comme vérité —
   * c'est le chemin à friction nulle quand la transcription était déjà bonne.
   */
  const validerCorrection = async (telQuel: boolean) => {
    if (!pageCorrectionId || validationEnCours) return
    setValidationEnCours(true)
    try {
      const res = await apiFetch(
        `${API}/encre/pages/${encodeURIComponent(pageCorrectionId)}/transcription/valider`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(telQuel ? {} : { texte: texteCorrection }),
        })
      if (!res.ok) {
        setEtatCorrection({ texte: await messageDErreur(res, 'Validation refusée'), erreur: true })
        return
      }
      setEtatCorrection({ texte: 'Exemple enregistré.', erreur: false })
      setRafraichirCorrection(n => n + 1)
    } catch {
      setEtatCorrection({ texte: 'Validation impossible : backend injoignable.', erreur: true })
    } finally {
      setValidationEnCours(false)
    }
  }

  const pagesAffichees = pagesACorreger.filter(p => afficherValidees || !p.validee_le)

  // ── Phase 3 : dictée inversée ─────────────────────────────────────────────

  //: Même déstructuration immédiate que le canvas de notes ci-dessus, et pour
  //: la même raison (cf. son commentaire).
  const {
    traits: traitsDictee, outil: outilDictee, setOutil: setOutilDictee,
    peutAnnuler: peutAnnulerDictee, peutRefaire: peutRefaireDictee,
    annuler: annulerDictee, refaire: refaireDictee, charger: chargerDictee,
    canvasRef: canvasRefDictee, zoneRef: zoneRefDictee,
    onPointerDown: onPointerDownDictee, onPointerMove: onPointerMoveDictee,
    terminer: terminerDictee,
  } = useDessinStylet()
  const [expressionDictee, setExpressionDictee] = useState<ExpressionDictee | null>(null)
  const [rafraichirDictee, setRafraichirDictee] = useState(0)
  const [etatDictee, setEtatDictee] = useState<Etat | null>(null)
  const [validationDicteeEnCours, setValidationDicteeEnCours] = useState(false)

  useEffect(() => {
    if (vue !== 'entrainement' || sousVue !== 'dictee') return
    let vivant = true
    apiFetch(`${API}/encre/entrainement/dictee/expression`)
      .then(r => r.json())
      .then(d => {
        if (!vivant) return
        const o = objet(d)
        const id = texte(o.id)
        // Un id vide viendrait d'un corps d'erreur (401 avant appairage, 404) —
        // rien à afficher plutôt qu'une expression fantôme.
        setExpressionDictee(id ? { id, latex: texte(o.latex) } : null)
      })
      .catch(() => { if (vivant) setExpressionDictee(null) })
    return () => { vivant = false }
  }, [vue, sousVue, rafraichirDictee])

  /** Change d'expression SANS valider — l'utilisateur passe une expression
   * trop difficile, elle reste dans la banque pour une prochaine fois. */
  const passerExpression = () => {
    chargerDictee([])
    setEtatDictee(null)
    setRafraichirDictee(n => n + 1)
  }

  const validerDictee = async () => {
    if (!expressionDictee || validationDicteeEnCours) return
    if (traitsDictee.length === 0) {
      setEtatDictee({ texte: 'Dessine la copie avant de valider.', erreur: true })
      return
    }
    setValidationDicteeEnCours(true)
    try {
      const res = await apiFetch(`${API}/encre/entrainement/dictee/valider`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expression_id: expressionDictee.id, strokes: traitsDictee }),
      })
      if (!res.ok) {
        setEtatDictee({ texte: await messageDErreur(res, 'Validation refusée'), erreur: true })
        return
      }
      setEtatDictee({ texte: 'Exemple enregistré.', erreur: false })
      chargerDictee([])
      setRafraichirDictee(n => n + 1)
    } catch {
      setEtatDictee({ texte: 'Validation impossible : backend injoignable.', erreur: true })
    } finally {
      setValidationDicteeEnCours(false)
    }
  }

  // ── Rendu ─────────────────────────────────────────────────────────────────

  const boutonOutil = (
    outilActif: 'stylo' | 'gomme', choisir: (o: 'stylo' | 'gomme') => void,
    valeur: 'stylo' | 'gomme', Icone: typeof PenLine, libelle: string,
  ) => (
    <button
      onClick={() => choisir(valeur)}
      title={libelle}
      aria-pressed={outilActif === valeur}
      className={`px-2 py-2 rounded-md text-sm transition-all duration-150 ${
        outilActif === valeur
          ? 'bg-gradient-primary text-on-accent shadow-sm'
          : 'bg-elevated border border-line text-secondary hover:text-primary'
      }`}
    >
      <Icone size={16} />
    </button>
  )

  return (
    <main className="flex flex-1 overflow-hidden">
      <aside className="w-64 shrink-0 border-r border-line flex flex-col overflow-hidden">
        <div className="px-4 py-3 flex items-center justify-between border-b border-line">
          <h1 className="text-sm font-semibold text-primary flex items-center gap-2">
            <PenTool size={16} className="text-accent" /> Encre
          </h1>
          {vue === 'notes' && (
            <button
              onClick={nouvellePage}
              title="Nouvelle page"
              className="px-2 py-1 rounded-md bg-gradient-primary text-on-accent text-xs shadow-sm hover:opacity-90 transition-all duration-150"
            >
              <Plus size={14} />
            </button>
          )}
        </div>
        {/* Phase 3 : deux vues du module, indépendantes de la page ouverte —
            basculer n'abandonne ni ne ferme la page de notes en cours, elle
            est simplement hors écran tant que `vue !== 'notes'`. */}
        <div role="tablist" aria-label="Vue du module" className="flex border-b border-line">
          <button
            role="tab"
            aria-selected={vue === 'notes'}
            onClick={() => setVue('notes')}
            className={`flex-1 px-3 py-2 text-xs flex items-center justify-center gap-1.5 transition-all duration-150 ${
              vue === 'notes' ? 'text-primary border-b-2 border-accent' : 'text-muted hover:text-secondary'
            }`}
          >
            <PenTool size={13} /> Notes
          </button>
          <button
            role="tab"
            aria-selected={vue === 'entrainement'}
            onClick={() => setVue('entrainement')}
            className={`flex-1 px-3 py-2 text-xs flex items-center justify-center gap-1.5 transition-all duration-150 ${
              vue === 'entrainement' ? 'text-primary border-b-2 border-accent' : 'text-muted hover:text-secondary'
            }`}
          >
            <GraduationCap size={13} /> Entraînement
          </button>
        </div>

        {vue === 'notes' ? (
          <ul className="flex-1 overflow-y-auto px-2 py-2 flex flex-col gap-1">
            {pages.length === 0 && (
              <li className="px-2 py-3 text-xs text-muted leading-relaxed">
                Aucune page. « + » en crée une.
              </li>
            )}
            {pages.map(p => (
              <li key={p.id} className="flex items-center gap-1">
                <button
                  onClick={() => ouvrirPage(p.id)}
                  className={`flex-1 text-left px-2 py-2 rounded-md text-xs transition-all duration-150 ${
                    p.id === pageId
                      ? 'bg-elevated text-primary border border-accent'
                      : 'text-secondary hover:bg-elevated'
                  }`}
                >
                  <span className="block truncate">{p.titre || 'Page sans titre'}</span>
                  <span className="block text-muted mt-0.5">
                    {p.n_traits} trait{p.n_traits > 1 ? 's' : ''}
                  </span>
                </button>
                <button
                  onClick={() => { void supprimerPage(p.id) }}
                  title="Supprimer cette page"
                  className="px-1.5 py-2 rounded-md text-muted hover:text-primary transition-all duration-150"
                >
                  <Trash2 size={13} />
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="flex-1 overflow-hidden flex flex-col">
            <div role="tablist" aria-label="Mode d'entraînement" className="flex gap-1 px-2 py-2">
              <button
                role="tab"
                aria-selected={sousVue === 'correction'}
                onClick={() => setSousVue('correction')}
                className={`flex-1 px-2 py-1.5 rounded-md text-xs transition-all duration-150 ${
                  sousVue === 'correction'
                    ? 'bg-gradient-primary text-on-accent shadow-sm'
                    : 'bg-elevated border border-line text-secondary hover:text-primary'
                }`}
              >
                Correction
              </button>
              <button
                role="tab"
                aria-selected={sousVue === 'dictee'}
                onClick={() => setSousVue('dictee')}
                className={`flex-1 px-2 py-1.5 rounded-md text-xs transition-all duration-150 ${
                  sousVue === 'dictee'
                    ? 'bg-gradient-primary text-on-accent shadow-sm'
                    : 'bg-elevated border border-line text-secondary hover:text-primary'
                }`}
              >
                Dictée
              </button>
            </div>
            {sousVue === 'correction' && (
              <>
                <label className="px-3 py-1.5 text-xs text-muted flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={afficherValidees}
                    onChange={e => setAfficherValidees(e.target.checked)}
                  />
                  Afficher aussi les pages déjà corrigées
                </label>
                <ul className="flex-1 overflow-y-auto px-2 py-1 flex flex-col gap-1">
                  {pagesAffichees.length === 0 && (
                    <li className="px-2 py-3 text-xs text-muted leading-relaxed">
                      Rien à corriger pour l&apos;instant.
                    </li>
                  )}
                  {pagesAffichees.map(p => (
                    <li key={p.id}>
                      <button
                        onClick={() => choisirPageCorrection(p)}
                        className={`w-full text-left px-2 py-2 rounded-md text-xs transition-all duration-150 ${
                          p.id === pageCorrectionId
                            ? 'bg-elevated text-primary border border-accent'
                            : 'text-secondary hover:bg-elevated'
                        }`}
                      >
                        <span className="block truncate">{p.titre || 'Page sans titre'}</span>
                        {p.validee_le && (
                          <span className="block text-muted mt-0.5">Déjà corrigée</span>
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {sousVue === 'dictee' && (
              <p className="px-3 py-2 text-xs text-muted leading-relaxed">
                Recopie l&apos;expression affichée sur le canvas, puis valide —
                aucune page à choisir, une nouvelle expression arrive à chaque
                validation.
              </p>
            )}
          </div>
        )}
      </aside>

      <section className="flex-1 flex flex-col overflow-hidden">
        {vue === 'entrainement' ? (
          sousVue === 'correction' ? (
            pageCorrectionId ? (
              <div className="flex-1 overflow-auto px-4 py-4 flex flex-col gap-3">
                <div className="bg-elevated border border-line rounded-md p-3">
                  <span className="text-xs text-muted uppercase tracking-wide">Aperçu</span>
                  <RenduLatex tex={texteCorrection} />
                </div>
                <textarea
                  value={texteCorrection}
                  onChange={e => setTexteCorrection(e.target.value)}
                  aria-label="Texte à corriger"
                  rows={8}
                  className="w-full bg-elevated border border-line rounded-md px-3 py-2 text-sm font-mono text-secondary resize-y focus:outline-none focus:border-accent"
                />
                <div className="bg-elevated border border-line rounded-md p-3">
                  <span className="text-xs text-muted uppercase tracking-wide">
                    Encre d&apos;origine
                  </span>
                  {/* Aperçu en lecture seule de la page manuscrite — pour
                      vérifier la transcription contre l'encre réelle plutôt
                      que de corriger à l'aveugle. */}
                  <div ref={zoneRefCorrection} className="mt-2">
                    <canvas
                      ref={canvasRefCorrection}
                      className="block rounded-md border border-line shadow-sm"
                    />
                  </div>
                </div>
                <div className="flex gap-2 flex-wrap">
                  <button
                    onClick={() => { void validerCorrection(true) }}
                    disabled={validationEnCours}
                    className="px-3 py-2 rounded-md bg-elevated border border-line text-sm text-secondary hover:text-primary disabled:opacity-40 transition-all duration-150"
                  >
                    Valider tel quel
                  </button>
                  <button
                    onClick={() => { void validerCorrection(false) }}
                    disabled={validationEnCours}
                    className="px-3 py-2 rounded-md bg-gradient-primary text-on-accent text-sm shadow-sm disabled:opacity-40 transition-all duration-150"
                  >
                    Valider la correction
                  </button>
                </div>
                {etatCorrection && (
                  <p
                    role="status"
                    className={`text-xs ${etatCorrection.erreur ? 'text-red-400' : 'text-muted'}`}
                  >
                    {etatCorrection.texte}
                  </p>
                )}
              </div>
            ) : (
              <div className="flex-1 flex items-center justify-center text-sm text-muted text-center px-8">
                Choisis une page à corriger dans la liste.
              </div>
            )
          ) : (
            <>
              <div className="px-4 py-3 border-b border-line flex items-center gap-2 flex-wrap">
                {boutonOutil(outilDictee, setOutilDictee, 'stylo', PenLine, 'Stylo')}
                {boutonOutil(outilDictee, setOutilDictee, 'gomme', Eraser, 'Gomme — supprime le trait sous le curseur')}
                <button
                  onClick={annulerDictee}
                  disabled={!peutAnnulerDictee}
                  title="Annuler"
                  className="px-2 py-2 rounded-md bg-elevated border border-line text-secondary hover:text-primary disabled:opacity-40 transition-all duration-150"
                >
                  <Undo2 size={16} />
                </button>
                <button
                  onClick={refaireDictee}
                  disabled={!peutRefaireDictee}
                  title="Rétablir"
                  className="px-2 py-2 rounded-md bg-elevated border border-line text-secondary hover:text-primary disabled:opacity-40 transition-all duration-150"
                >
                  <Redo2 size={16} />
                </button>
                <button
                  onClick={passerExpression}
                  title="Passer à une autre expression sans valider"
                  className="px-3 py-2 rounded-md bg-elevated border border-line text-sm text-secondary hover:text-primary transition-all duration-150 flex items-center gap-1.5"
                >
                  <SkipForward size={15} /> Nouvelle expression
                </button>
                <button
                  onClick={() => { void validerDictee() }}
                  disabled={validationDicteeEnCours || traitsDictee.length === 0}
                  className="px-3 py-2 rounded-md bg-gradient-primary text-on-accent text-sm shadow-sm disabled:opacity-40 transition-all duration-150 flex items-center gap-1.5"
                >
                  <CheckCircle2 size={15} /> Valider
                </button>
              </div>
              {expressionDictee && (
                <div className="px-4 py-3 border-b border-line">
                  <span className="text-xs text-muted uppercase tracking-wide">À recopier</span>
                  <RenduLatex tex={expressionDictee.latex} />
                </div>
              )}
              {etatDictee && (
                <p
                  role="status"
                  className={`px-4 py-2 text-xs ${etatDictee.erreur ? 'text-red-400' : 'text-muted'}`}
                >
                  {etatDictee.texte}
                </p>
              )}
              <div ref={zoneRefDictee} className="flex-1 overflow-auto px-4 py-4">
                <canvas
                  ref={canvasRefDictee}
                  onPointerDown={onPointerDownDictee}
                  onPointerMove={onPointerMoveDictee}
                  onPointerUp={terminerDictee}
                  onPointerCancel={terminerDictee}
                  onPointerLeave={terminerDictee}
                  style={{ touchAction: 'none' }}
                  className="block rounded-md border border-line shadow-sm cursor-crosshair"
                />
              </div>
            </>
          )
        ) : pageId ? (
          <>
            <div className="px-4 py-3 border-b border-line flex items-center gap-2 flex-wrap">
              <input
                value={titre}
                onChange={e => setTitre(e.target.value)}
                placeholder="Titre de la page"
                aria-label="Titre de la page"
                className="flex-1 min-w-40 bg-elevated border border-line rounded-md px-3 py-2 text-sm text-primary placeholder-muted focus:outline-none focus:border-accent"
              />
              {boutonOutil(outil, setOutil, 'stylo', PenLine, 'Stylo')}
              {boutonOutil(outil, setOutil, 'gomme', Eraser, 'Gomme — supprime le trait sous le curseur')}
              <button
                onClick={annuler}
                disabled={!peutAnnuler}
                title="Annuler"
                className="px-2 py-2 rounded-md bg-elevated border border-line text-secondary hover:text-primary disabled:opacity-40 transition-all duration-150"
              >
                <Undo2 size={16} />
              </button>
              <button
                onClick={refaire}
                disabled={!peutRefaire}
                title="Rétablir"
                className="px-2 py-2 rounded-md bg-elevated border border-line text-secondary hover:text-primary disabled:opacity-40 transition-all duration-150"
              >
                <Redo2 size={16} />
              </button>
              <button
                onClick={() => { void enregistrerMaintenant() }}
                className="px-3 py-2 rounded-md bg-elevated border border-line text-sm text-secondary hover:text-primary transition-all duration-150"
              >
                {sale ? 'Enregistrer' : 'Enregistré'}
              </button>
              {/* Le mode se change à tout moment, indépendamment de la
                  transcription déjà présente ou non : c'est une propriété de
                  la page, pas un état du bouton « Transcrire ». */}
              <div
                role="group"
                aria-label="Mode de la page"
                className="flex items-center gap-0.5 bg-elevated border border-line rounded-md p-0.5"
              >
                <button
                  onClick={() => setMode('maths')}
                  aria-pressed={mode === 'maths'}
                  title="Mode maths — pix2text-mfr transcrit les formules en LaTeX"
                  className={`px-2 py-1.5 rounded text-xs flex items-center gap-1 transition-all duration-150 ${
                    mode === 'maths'
                      ? 'bg-gradient-primary text-on-accent shadow-sm'
                      : 'text-secondary hover:text-primary'
                  }`}
                >
                  <Sigma size={13} /> Maths
                </button>
                <button
                  onClick={() => setMode('lettres')}
                  aria-pressed={mode === 'lettres'}
                  title="Mode lettres — transcription désactivée : pix2text-mfr ne reconnaît que des formules mathématiques, pas du texte manuscrit courant"
                  className={`px-2 py-1.5 rounded text-xs flex items-center gap-1 transition-all duration-150 ${
                    mode === 'lettres'
                      ? 'bg-gradient-primary text-on-accent shadow-sm'
                      : 'text-secondary hover:text-primary'
                  }`}
                >
                  <Type size={13} /> Lettres
                </button>
              </div>
              <button
                onClick={() => { void transcrire() }}
                // Trois raisons de désactiver, et elles ne se confondent pas :
                // sans encre (le backend répondrait 400, et proposer un bouton
                // dont on connaît d'avance le refus est une invitation à un
                // message d'erreur), pendant l'appel (le modèle tourne sur le
                // CPU et peut prendre des dizaines de secondes au premier
                // usage), et en mode "lettres" — cf. le texte explicatif juste
                // en dessous, un `title` seul ne suffit pas à en avertir avant
                // le clic.
                disabled={transcrit || traits.length === 0 || mode === 'lettres'}
                title={
                  mode === 'lettres'
                    ? 'Transcription désactivée en mode lettres'
                    : "Transcrire l'encre en LaTeX et l'ajouter aux fiches cherchables"
                }
                className="px-3 py-2 rounded-md bg-elevated border border-line text-sm text-secondary hover:text-primary disabled:opacity-40 transition-all duration-150 flex items-center gap-1.5"
              >
                <ScanText size={15} />
                {transcrit ? 'Transcription…' : 'Transcrire'}
              </button>
              {mode === 'lettres' && (
                <p className="w-full text-xs text-muted leading-relaxed">
                  Transcription désactivée en mode lettres : pix2text-mfr est un
                  reconnaisseur de formules mathématiques, pas un OCR généraliste —
                  passe en mode « Maths » si cette page contient des formules.
                </p>
              )}
            </div>

            {transcription && (
              <div className="px-4 py-3 border-b border-line flex flex-col gap-1.5">
                <div className="flex items-baseline justify-between gap-2 flex-wrap">
                  <span className="text-xs text-muted uppercase tracking-wide">
                    Transcription
                  </span>
                  {/* Modèle, version et date sont AFFICHÉS, pas seulement
                      stockés : c'est la seule information qui explique pourquoi
                      deux pages transcrites à trois mois d'écart ne se
                      ressemblent pas. */}
                  <span className="text-xs font-mono text-muted">
                    {transcription.modele} · {transcription.version} ·{' '}
                    {transcription.date.replace('T', ' à ')}
                  </span>
                </div>
                {transcription.texte ? (
                  // `readOnly` et non `disabled` : le texte reste sélectionnable
                  // et copiable, ce qui est tout l'usage qu'on en a ici. La
                  // correction est la phase 3.
                  <textarea
                    readOnly
                    value={transcription.texte}
                    aria-label="Texte transcrit (lecture seule)"
                    rows={Math.min(6, transcription.texte.split('\n').length + 1)}
                    className="w-full bg-elevated border border-line rounded-md px-3 py-2 text-xs font-mono text-secondary resize-y focus:outline-none focus:border-accent"
                  />
                ) : (
                  <p className="text-xs text-muted">
                    Le modèle n&apos;a rien reconnu sur cette page.
                  </p>
                )}
              </div>
            )}

            {etat && (
              <p
                role="status"
                className={`px-4 py-2 text-xs ${etat.erreur ? 'text-red-400' : 'text-muted'}`}
              >
                {etat.texte}
              </p>
            )}

            <div ref={zoneRef} className="flex-1 overflow-auto px-4 py-4">
              <canvas
                ref={canvasRef}
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={terminer}
                onPointerCancel={terminer}
                onPointerLeave={terminer}
                // `touch-action: none` en style INLINE et non en classe Tailwind :
                // sans lui le navigateur avale le geste comme un défilement et
                // n'envoie plus d'événement après quelques pixels.
                style={{ touchAction: 'none' }}
                className="block rounded-md border border-line shadow-sm cursor-crosshair"
              />
            </div>
          </>
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center gap-3 px-8 text-center">
            <PenTool size={28} className="text-muted" />
            <p className="text-sm text-secondary max-w-sm leading-relaxed">
              Prise de notes au stylet. Les tracés sont conservés sous forme
              vectorielle — pression et temps compris — et jamais sous forme
              d&apos;image.
            </p>
            {etat && (
              <p role="status" className={`text-xs ${etat.erreur ? 'text-red-400' : 'text-muted'}`}>
                {etat.texte}
              </p>
            )}
            <button
              onClick={nouvellePage}
              className="px-3 py-2 rounded-md bg-gradient-primary text-on-accent text-sm shadow-sm hover:opacity-90 transition-all duration-150"
            >
              Nouvelle page
            </button>
          </div>
        )}
      </section>
    </main>
  )
}
