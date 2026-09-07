import { useEffect, useRef, useState } from 'react'
import { Eraser, PenLine, PenTool, Plus, Redo2, Trash2, Undo2 } from 'lucide-react'
import { getStroke } from 'perfect-freehand'
import type { SharedModuleProps } from '../../registry'
import { API, apiFetch } from '../../../api'
import { dico, liste, texte } from '../../../normaliser'

/**
 * Module « encre » — prise de notes manuscrites au stylet. Phase 1 : zéro ML.
 *
 * Feuille de route complète dans `docs/module-encre.md`. Ce composant n'en
 * réalise que la première phase, dont la valeur est autonome : une page où
 * écrire, une liste de pages, rien qui prépare la transcription. Aucun appel
 * LLM n'existe sur ce chemin, dans aucune branche.
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

//: Taille logique d'une page, en unités de coordonnées. Le rapport est celui
//: d'une A4 en portrait ; les valeurs elles-mêmes n'ont d'importance que par
//: leur constance — elles sont figées dans les fichiers déjà écrits, donc les
//: changer déplacerait l'encre existante.
const LARGEUR = 1240
const HAUTEUR = 1754

//: Encre et papier. Une seule couleur et une seule épaisseur : la phase 1 n'a
//: pas de palette, et en ajouter une n'est pas dans son périmètre. Les deux
//: valeurs sont malgré tout écrites DANS chaque trait plutôt que déduites à
//: l'affichage — le jour où une palette arrive, les pages déjà écrites n'auront
//: pas à être migrées, et une page ancienne gardera l'aspect qu'elle avait.
const COULEUR_ENCRE = '#111827'
const TAILLE_TRAIT = 3.5

//: Le papier reste blanc dans les deux thèmes. Ce n'est pas un oubli : la
//: couleur de l'encre est une donnée de la page, donc inverser le fond en thème
//: sombre rendrait invisible une encre foncée écrite la veille. Le contour de la
//: zone, lui, suit le thème.
const PAPIER = '#ffffff'

//: Rayon de la gomme, en unités logiques. La gomme supprime le TRAIT sous le
//: curseur — pas des pixels : les traits sont des vecteurs, il n'y a pas de
//: pixels à effacer, et une gomme par pixel obligerait à découper un trait en
//: deux, donc à inventer une notion de trait partiel que la phase 2 devrait
//: ensuite savoir transcrire.
const RAYON_GOMME = 14

//: Réglage de `perfect-freehand`. `thinning` est ce qui rend le trait sensible à
//: la pression ; `simulatePressure: false` parce qu'un stylet actif FOURNIT la
//: pression — la simuler à partir de la vitesse par-dessus une vraie mesure
//: donne un trait qui ne suit pas la main.
const RENDU = { thinning: 0.6, smoothing: 0.5, streamline: 0.5, simulatePressure: false }

//: Délai d'inactivité avant l'enregistrement automatique. Assez court pour
//: qu'une pause entre deux mots suffise, assez long pour ne pas envoyer une
//: requête par trait.
const DELAI_ENREGISTREMENT_MS = 1200

//: Profondeur de la pile d'annulation. Les instantanés partagent leurs traits
//: (ce sont les mêmes objets), donc le coût est celui des références, pas celui
//: de l'encre — la borne est là pour éviter une croissance non bornée sur une
//: séance de plusieurs heures, pas pour économiser de la mémoire.
const PROFONDEUR_HISTORIQUE = 60

interface Point {
  x: number
  y: number
  /** 0…1 tel que rendu par le stylet ; 0,5 par défaut pour une souris. */
  pression: number
  /**
   * Millisecondes depuis le début DU TRAIT.
   *
   * `docs/module-encre.md` désigne `(x, y, t, pression)` comme la donnée
   * irremplaçable, et le temps en fait partie : c'est lui qui porte la vitesse
   * du geste, dont dépendent la segmentation et le rendu d'un tracé. Il ne sert
   * à rien aujourd'hui — le rendu n'en a pas besoin — et c'est exactement pour
   * ça qu'il faut l'écrire maintenant : on ne le retrouvera jamais après coup.
   * Relatif au trait et non absolu, pour ne pas semer un horodatage
   * d'utilisation dans chaque point.
   */
  t: number
}

interface Trait {
  couleur: string
  taille: number
  points: Point[]
}

interface PageResume {
  id: string
  titre: string
  date_creation: string
  date_modification: string
  n_traits: number
}

/** Résultat d'un appel d'écriture, tel qu'affiché dans le bandeau d'état. */
interface Etat {
  texte: string
  erreur: boolean
}

/**
 * Ce que le serveur a confirmé avoir enregistré. `null` = aucune page chargée.
 *
 * C'est un instantané par RÉFÉRENCE, pas une copie : `traits` y est le tableau
 * exact qui a été envoyé. La comparaison « y a-t-il quelque chose à
 * enregistrer ? » est donc une égalité de références, et elle est juste parce
 * que toute modification de l'encre passe par `appliquer()`, qui construit un
 * NOUVEAU tableau. Voir `sale` plus bas pour ce que ça évite.
 */
interface Instantane {
  titre: string
  traits: Trait[]
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
async function sauvegarder(pageId: string, titre: string, traits: Trait[]): Promise<Etat> {
  try {
    const res = await apiFetch(`${API}/encre/pages/${encodeURIComponent(pageId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ titre, strokes: traits }),
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

// ── Géométrie et rendu ───────────────────────────────────────────────────────

/**
 * Ajuste le canvas à la largeur disponible. Rend `false` s'il n'y a rien à
 * dessiner (zone de largeur nulle : panneau replié, ou jsdom en test).
 *
 * Trois tailles cohabitent et les confondre donne un tracé flou ou décalé :
 * la taille LOGIQUE (`LARGEUR`×`HAUTEUR`, dans laquelle vivent les points), la
 * taille CSS (ce que l'utilisateur voit), et la taille en pixels du canvas
 * (CSS × `devicePixelRatio`, la finesse réelle du rendu). Seule la première est
 * stockée ; les deux autres se recalculent à chaque redimensionnement.
 */
function dimensionner(canvas: HTMLCanvasElement, zone: HTMLElement): boolean {
  const largeurCss = zone.clientWidth
  if (largeurCss <= 0) return false
  const hauteurCss = (largeurCss * HAUTEUR) / LARGEUR
  const dpr = window.devicePixelRatio || 1
  canvas.style.width = `${largeurCss}px`
  canvas.style.height = `${hauteurCss}px`
  canvas.width = Math.max(1, Math.round(largeurCss * dpr))
  canvas.height = Math.max(1, Math.round(hauteurCss * dpr))
  return true
}

/** Le contour d'un trait, prêt à remplir. `null` si le trait ne produit rien. */
function chemin(trait: Trait): Path2D | null {
  const contour = getStroke(
    trait.points.map(p => [p.x, p.y, p.pression]),
    { ...RENDU, size: trait.taille },
  )
  if (contour.length < 3) return null
  const p = new Path2D()
  p.moveTo(contour[0][0], contour[0][1])
  for (let i = 1; i < contour.length; i++) p.lineTo(contour[i][0], contour[i][1])
  p.closePath()
  return p
}

/**
 * Repeint la page entière : papier, traits enregistrés, puis le trait en cours.
 *
 * Tout est redessiné à chaque trame plutôt que d'entretenir un calque des traits
 * déjà posés. C'est le choix simple, et il est assumé pour cette phase : une
 * page de notes tient dans quelques centaines de traits, et un calque
 * introduirait un second état à garder cohérent avec `traits` — exactement le
 * genre de doublon qui finit par diverger. Si une page réelle devient lente,
 * c'est ici que ça se corrige, et la mesure existera alors.
 *
 * `getContext('2d')` peut rendre `null` (jsdom sans canvas, contexte perdu) :
 * on sort, on ne lève pas. Un module dont le rendu jette casserait la page
 * entière — `ModuleErrorBoundary` n'attrape pas les erreurs d'un gestionnaire
 * d'événements.
 */
function peindre(canvas: HTMLCanvasElement, traits: Trait[], enCours: Trait | null): void {
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const k = canvas.width / LARGEUR
  ctx.setTransform(k, 0, 0, k, 0, 0)
  ctx.clearRect(0, 0, LARGEUR, HAUTEUR)
  ctx.fillStyle = PAPIER
  ctx.fillRect(0, 0, LARGEUR, HAUTEUR)
  for (const trait of enCours ? [...traits, enCours] : traits) {
    const forme = chemin(trait)
    if (!forme) continue
    ctx.fillStyle = trait.couleur
    ctx.fill(forme)
  }
}

/** Coordonnées logiques d'un événement, quelle que soit l'échelle d'affichage. */
function versLogique(canvas: HTMLCanvasElement, e: PointerEvent): { x: number; y: number } {
  const r = canvas.getBoundingClientRect()
  if (r.width <= 0 || r.height <= 0) return { x: 0, y: 0 }
  return {
    x: ((e.clientX - r.left) / r.width) * LARGEUR,
    y: ((e.clientY - r.top) / r.height) * HAUTEUR,
  }
}

/** Le trait est-il sous la gomme ? Distance au point le plus proche. */
function souLaGomme(trait: Trait, x: number, y: number): boolean {
  return trait.points.some(p => Math.hypot(p.x - x, p.y - y) <= RAYON_GOMME)
}

// ── Composant ────────────────────────────────────────────────────────────────

export default function EncreModule(_props: SharedModuleProps) {
  const [pages, setPages] = useState<PageResume[]>([])
  const [pageId, setPageId] = useState('')
  const [titre, setTitre] = useState('')
  const [traits, setTraits] = useState<Trait[]>([])
  //: Instantanés de `traits`, pas une pile de traits ajoutés. C'est ce qui rend
  //: la gomme annulable au même titre que l'écriture : une pile de traits ne
  //: saurait pas restaurer une suppression. Les instantanés partagent leurs
  //: objets `Trait`, donc ils ne coûtent que des références.
  const [passe, setPasse] = useState<Trait[][]>([])
  const [futur, setFutur] = useState<Trait[][]>([])
  const [outil, setOutil] = useState<'stylo' | 'gomme'>('stylo')
  //: Dernier état CONFIRMÉ par le serveur. Cf. `Instantane` et `sale`.
  const [enregistre, setEnregistre] = useState<Instantane | null>(null)
  const [etat, setEtat] = useState<Etat | null>(null)
  //: Incrémenté pour redemander la liste. Un compteur plutôt qu'une fonction de
  //: rechargement : une fonction devrait figurer dans les dépendances de l'effet
  //: qui charge, et y changerait à chaque rendu.
  const [rafraichir, setRafraichir] = useState(0)

  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const zoneRef = useRef<HTMLDivElement | null>(null)
  //: Le trait en cours de tracé. Dans une ref et NON dans l'état : un stylet
  //: produit plusieurs centaines de points par seconde, et un rendu React par
  //: point rendrait l'écriture inutilisable. Il est peint impérativement, puis
  //: versé dans `traits` une seule fois, au relevé du stylet.
  const enCoursRef = useRef<Trait | null>(null)
  const pointeurRef = useRef<number | null>(null)
  const typeEnCoursRef = useRef<string>('')
  const debutRef = useRef(0)
  //: Un stylet est-il en contact ? Tant que oui, tout événement tactile est
  //: ignoré — c'est la paume posée sur l'écran.
  const styletActifRef = useRef(false)
  const trameRef = useRef(0)

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
    && (enregistre.titre !== titre || enregistre.traits !== traits)

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
        setTitre(titreCharge)
        setTraits(traitsCharges)
        setPasse([])
        setFutur([])
        // LES MÊMES objets que ceux posés dans l'état : l'instantané se compare
        // par référence, en poser une copie rendrait la page sale d'emblée et
        // déclencherait un enregistrement inutile à chaque ouverture.
        setEnregistre({ titre: titreCharge, traits: traitsCharges })
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
  }, [pageId])

  // ── Dimensionnement et rendu ──────────────────────────────────────────────
  //
  // Un seul effet pour les deux, et il dépend de `traits` : redimensionner
  // remet à zéro le contenu du canvas (toute écriture de `canvas.width` l'efface),
  // donc les deux opérations sont indissociables et les séparer laisserait une
  // page blanche après chaque redimensionnement.
  useEffect(() => {
    const canvas = canvasRef.current
    const zone = zoneRef.current
    if (!canvas || !zone) return
    const ajuster = () => {
      if (dimensionner(canvas, zone)) peindre(canvas, traits, enCoursRef.current)
    }
    ajuster()
    // jsdom n'implémente pas ResizeObserver : le test en pose un bouchon. Le
    // garde ici couvrirait aussi un navigateur trop ancien, où la page reste
    // simplement figée à sa taille initiale au lieu de ne pas s'afficher.
    if (typeof ResizeObserver === 'undefined') return
    const observateur = new ResizeObserver(ajuster)
    observateur.observe(zone)
    return () => observateur.disconnect()
  }, [traits])

  // ── Enregistrement automatique ────────────────────────────────────────────
  useEffect(() => {
    if (!sale || !pageId) return
    const minuteur = setTimeout(() => {
      // Capturé AVANT l'envoi : c'est exactement ce que le serveur va recevoir,
      // donc exactement ce qu'on aura le droit de dire « enregistré ».
      const envoye: Instantane = { titre, traits }
      void sauvegarder(pageId, envoye.titre, envoye.traits).then(resultat => {
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
  }, [sale, pageId, titre, traits])

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
    setTraits([])
    setPasse([])
    setFutur([])
    setTitre('')
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
    const enAttente = sale && pageId ? { pageId, titre, traits } : null
    reinitialiser()
    setPageId(id)
    if (enAttente) {
      void sauvegarder(enAttente.pageId, enAttente.titre, enAttente.traits)
        .then(resultat => { if (resultat.erreur) setEtat(resultat) })
    }
  }

  // ── Mutations de l'encre ──────────────────────────────────────────────────

  /** Point d'entrée UNIQUE de toute modification des traits.
   *
   * Empile l'état précédent, vide la pile de rétablissement et marque la page
   * comme non enregistrée — trois choses qu'un appelant qui ferait `setTraits`
   * directement oublierait tôt ou tard, et dont l'oubli le plus probable (le
   * marquage) donne le pire symptôme : une modification qui ne part jamais.
   */
  const appliquer = (suivant: Trait[]) => {
    setPasse(p => [...p, traits].slice(-PROFONDEUR_HISTORIQUE))
    setFutur([])
    setTraits(suivant)
  }

  const annuler = () => {
    if (passe.length === 0) return
    setFutur(f => [traits, ...f])
    setTraits(passe[passe.length - 1])
    setPasse(p => p.slice(0, -1))
  }

  const refaire = () => {
    if (futur.length === 0) return
    setPasse(p => [...p, traits].slice(-PROFONDEUR_HISTORIQUE))
    setTraits(futur[0])
    setFutur(f => f.slice(1))
  }

  // ── Événements du stylet ──────────────────────────────────────────────────

  /** Repeint hors du cycle React, au plus une fois par trame. */
  const repeindre = () => {
    if (trameRef.current) return
    trameRef.current = requestAnimationFrame(() => {
      trameRef.current = 0
      const canvas = canvasRef.current
      if (canvas) peindre(canvas, traits, enCoursRef.current)
    })
  }

  const abandonner = () => {
    enCoursRef.current = null
    pointeurRef.current = null
    typeEnCoursRef.current = ''
    repeindre()
  }

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const natif = e.nativeEvent
    if (natif.pointerType === 'touch' && styletActifRef.current) return
    if (natif.pointerType === 'pen') {
      styletActifRef.current = true
      // La paume touche l'écran AVANT la pointe : le trait qu'elle a commencé
      // est abandonné, pas seulement interrompu. Sans ça, la règle « ignorer le
      // tactile pendant qu'un stylet écrit » arrive toujours trop tard.
      if (typeEnCoursRef.current === 'touch') abandonner()
    }
    const canvas = canvasRef.current
    if (!canvas) return
    const { x, y } = versLogique(canvas, natif)

    if (outil === 'gomme') {
      pointeurRef.current = natif.pointerId
      typeEnCoursRef.current = natif.pointerType
      canvas.setPointerCapture(natif.pointerId)
      const restants = traits.filter(t => !souLaGomme(t, x, y))
      if (restants.length !== traits.length) appliquer(restants)
      return
    }

    pointeurRef.current = natif.pointerId
    typeEnCoursRef.current = natif.pointerType
    debutRef.current = natif.timeStamp
    canvas.setPointerCapture(natif.pointerId)
    enCoursRef.current = {
      couleur: COULEUR_ENCRE,
      taille: TAILLE_TRAIT,
      points: [{ x, y, pression: natif.pressure > 0 ? natif.pressure : 0.5, t: 0 }],
    }
    repeindre()
  }

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const natif = e.nativeEvent
    if (pointeurRef.current !== natif.pointerId) return
    const canvas = canvasRef.current
    if (!canvas) return

    // LE point du stylet : un `pointermove` en regroupe plusieurs. Sans cet
    // appel, la moitié des points d'un stylet à 240 Hz est perdue et les
    // courbes rapides deviennent des segments. Le test de présence couvre
    // jsdom et les navigateurs qui ne l'implémentent pas.
    const bruts = typeof natif.getCoalescedEvents === 'function'
      ? natif.getCoalescedEvents()
      : [natif]
    const echantillons = bruts.length > 0 ? bruts : [natif]

    if (outil === 'gomme') {
      let restants = traits
      for (const ev of echantillons) {
        const { x, y } = versLogique(canvas, ev)
        restants = restants.filter(t => !souLaGomme(t, x, y))
      }
      if (restants.length !== traits.length) appliquer(restants)
      return
    }

    const enCours = enCoursRef.current
    if (!enCours) return
    for (const ev of echantillons) {
      const { x, y } = versLogique(canvas, ev)
      enCours.points.push({
        x, y,
        pression: ev.pressure > 0 ? ev.pressure : 0.5,
        t: Math.round(ev.timeStamp - debutRef.current),
      })
    }
    repeindre()
  }

  const terminer = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const natif = e.nativeEvent
    if (natif.pointerType === 'pen') styletActifRef.current = false
    if (pointeurRef.current !== natif.pointerId) return
    const enCours = enCoursRef.current
    // La ref est vidée AVANT `appliquer` : sinon l'effet de rendu, déclenché par
    // le changement de `traits`, dessinerait le trait deux fois.
    enCoursRef.current = null
    pointeurRef.current = null
    typeEnCoursRef.current = ''
    if (enCours && enCours.points.length > 0) appliquer([...traits, enCours])
    else repeindre()
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
    const envoye: Instantane = { titre, traits }
    const resultat = await sauvegarder(pageId, envoye.titre, envoye.traits)
    setEtat(resultat)
    if (!resultat.erreur) {
      setEnregistre(envoye)
      setRafraichir(n => n + 1)
    }
  }

  // ── Rendu ─────────────────────────────────────────────────────────────────

  const boutonOutil = (valeur: 'stylo' | 'gomme', Icone: typeof PenLine, libelle: string) => (
    <button
      onClick={() => setOutil(valeur)}
      title={libelle}
      aria-pressed={outil === valeur}
      className={`px-2 py-2 rounded-md text-sm transition-all duration-150 ${
        outil === valeur
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
          <button
            onClick={nouvellePage}
            title="Nouvelle page"
            className="px-2 py-1 rounded-md bg-gradient-primary text-on-accent text-xs shadow-sm hover:opacity-90 transition-all duration-150"
          >
            <Plus size={14} />
          </button>
        </div>
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
      </aside>

      <section className="flex-1 flex flex-col overflow-hidden">
        {pageId ? (
          <>
            <div className="px-4 py-3 border-b border-line flex items-center gap-2 flex-wrap">
              <input
                value={titre}
                onChange={e => setTitre(e.target.value)}
                placeholder="Titre de la page"
                aria-label="Titre de la page"
                className="flex-1 min-w-40 bg-elevated border border-line rounded-md px-3 py-2 text-sm text-primary placeholder-muted focus:outline-none focus:border-accent"
              />
              {boutonOutil('stylo', PenLine, 'Stylo')}
              {boutonOutil('gomme', Eraser, 'Gomme — supprime le trait sous le curseur')}
              <button
                onClick={annuler}
                disabled={passe.length === 0}
                title="Annuler"
                className="px-2 py-2 rounded-md bg-elevated border border-line text-secondary hover:text-primary disabled:opacity-40 transition-all duration-150"
              >
                <Undo2 size={16} />
              </button>
              <button
                onClick={refaire}
                disabled={futur.length === 0}
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
            </div>

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
