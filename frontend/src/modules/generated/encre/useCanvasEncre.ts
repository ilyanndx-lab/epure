import { useCallback, useEffect, useRef, useState } from 'react'
import { getStroke } from 'perfect-freehand'

/**
 * Mécanique du canvas à stylet — extraite de `Component.tsx` (phase 3) pour
 * être partagée entre la prise de notes (page persistée, autosave) et la
 * dictée inversée (canvas éphémère, aucune persistance) sans dupliquer les
 * trois pièges du stylet, chacun payé une fois par quelqu'un d'autre avant
 * nous :
 *
 * 1. **`getCoalescedEvents()`**. Un stylet actif échantillonne à 240 Hz ; le
 *    navigateur ne délivre qu'un `pointermove` par trame (60 Hz) et REGROUPE
 *    les autres dedans. Sans lui, les courbes rapides deviennent des segments.
 * 2. **`touch-action: none`** (posé par l'appelant, en style inline sur le
 *    `<canvas>` — ce fichier ne rend rien, il ne peut pas le poser lui-même).
 * 3. **Le rejet de la paume** : tant qu'un stylet est en contact, tout
 *    `pointerType === 'touch'` est ignoré ; et quand un stylet se pose alors
 *    qu'un trait tactile a déjà commencé, ce trait est ABANDONNÉ — sans la
 *    seconde règle, la première arrive trop tard.
 *
 * `Component.tsx` (notes, page 1) et le futur canvas de dictée inversée
 * (phase 3) appellent tous deux `useDessinStylet` ; seule la PERSISTANCE
 * diffère (autosave + chargement d'une page contre un canvas qui se vide au
 * clic de « Valider »), et elle reste du ressort de l'appelant.
 */

//: Taille logique d'une page, en unités de coordonnées. Le rapport est celui
//: d'une A4 en portrait ; les valeurs elles-mêmes n'ont d'importance que par
//: leur constance — elles sont figées dans les fichiers déjà écrits (notes),
//: donc les changer déplacerait l'encre existante.
export const LARGEUR = 1240
export const HAUTEUR = 1754

//: Encre et papier. Une seule couleur et une seule épaisseur : ni la prise de
//: notes ni la dictée n'ont de palette. Les deux valeurs sont malgré tout
//: écrites DANS chaque trait plutôt que déduites à l'affichage — le jour où
//: une palette arrive, les pages déjà écrites n'auront pas à être migrées.
export const COULEUR_ENCRE = '#111827'
export const TAILLE_TRAIT = 3.5

//: Le papier reste blanc dans les deux thèmes : la couleur de l'encre est une
//: donnée de la page, inverser le fond en thème sombre rendrait invisible une
//: encre foncée écrite la veille.
export const PAPIER = '#ffffff'

//: Rayon de la gomme, en unités logiques. Elle supprime le TRAIT sous le
//: curseur — pas des pixels : les traits sont des vecteurs.
export const RAYON_GOMME = 14

//: Réglage de `perfect-freehand`. `thinning` rend le trait sensible à la
//: pression ; `simulatePressure: false` parce qu'un stylet actif FOURNIT la
//: pression — la simuler à partir de la vitesse par-dessus une vraie mesure
//: donne un trait qui ne suit pas la main.
export const RENDU = { thinning: 0.6, smoothing: 0.5, streamline: 0.5, simulatePressure: false }

//: Profondeur de la pile d'annulation. Les instantanés partagent leurs traits
//: (mêmes objets), donc le coût est celui des références, pas celui de
//: l'encre — la borne évite une croissance non bornée sur une longue séance.
export const PROFONDEUR_HISTORIQUE = 60

export interface Point {
  x: number
  y: number
  /** 0…1 tel que rendu par le stylet ; 0,5 par défaut pour une souris. */
  pression: number
  /** Millisecondes depuis le début DU TRAIT — relatif, pas un horodatage
   * d'utilisation semé dans chaque point. */
  t: number
}

export interface Trait {
  couleur: string
  taille: number
  points: Point[]
}

/**
 * Ajuste le canvas à la largeur disponible. Rend `false` s'il n'y a rien à
 * dessiner (zone de largeur nulle : panneau replié, ou jsdom en test).
 *
 * Trois tailles cohabitent et les confondre donne un tracé flou ou décalé :
 * la taille LOGIQUE (`largeur`×`hauteur`, dans laquelle vivent les points), la
 * taille CSS (ce que l'utilisateur voit), et la taille en pixels du canvas
 * (CSS × `devicePixelRatio`, la finesse réelle du rendu). Seule la première
 * est stockée ; les deux autres se recalculent à chaque redimensionnement.
 */
export function dimensionner(
  canvas: HTMLCanvasElement, zone: HTMLElement, largeur: number, hauteur: number,
): boolean {
  const largeurCss = zone.clientWidth
  if (largeurCss <= 0) return false
  const hauteurCss = (largeurCss * hauteur) / largeur
  const dpr = window.devicePixelRatio || 1
  canvas.style.width = `${largeurCss}px`
  canvas.style.height = `${hauteurCss}px`
  canvas.width = Math.max(1, Math.round(largeurCss * dpr))
  canvas.height = Math.max(1, Math.round(hauteurCss * dpr))
  return true
}

/** Le contour d'un trait, prêt à remplir. `null` si le trait ne produit rien. */
export function chemin(trait: Trait): Path2D | null {
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
 * Tout est redessiné à chaque trame plutôt que d'entretenir un calque des
 * traits déjà posés — le choix simple, assumé pour un canvas qui tient dans
 * quelques centaines de traits (notes) ou une seule expression (dictée).
 *
 * `getContext('2d')` peut rendre `null` (jsdom sans canvas, contexte perdu) :
 * on sort, on ne lève pas.
 */
export function peindre(
  canvas: HTMLCanvasElement, largeur: number, hauteur: number,
  traits: Trait[], enCours: Trait | null,
): void {
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const k = canvas.width / largeur
  ctx.setTransform(k, 0, 0, k, 0, 0)
  ctx.clearRect(0, 0, largeur, hauteur)
  ctx.fillStyle = PAPIER
  ctx.fillRect(0, 0, largeur, hauteur)
  for (const trait of enCours ? [...traits, enCours] : traits) {
    const forme = chemin(trait)
    if (!forme) continue
    ctx.fillStyle = trait.couleur
    ctx.fill(forme)
  }
}

/** Coordonnées logiques d'un événement, quelle que soit l'échelle d'affichage. */
export function versLogique(
  canvas: HTMLCanvasElement, e: PointerEvent, largeur: number, hauteur: number,
): { x: number; y: number } {
  const r = canvas.getBoundingClientRect()
  if (r.width <= 0 || r.height <= 0) return { x: 0, y: 0 }
  return {
    x: ((e.clientX - r.left) / r.width) * largeur,
    y: ((e.clientY - r.top) / r.height) * hauteur,
  }
}

/** Le trait est-il sous la gomme ? Distance au point le plus proche. */
export function souLaGomme(trait: Trait, x: number, y: number): boolean {
  return trait.points.some(p => Math.hypot(p.x - x, p.y - y) <= RAYON_GOMME)
}

export interface OptionsDessinStylet {
  largeur?: number
  hauteur?: number
}

export interface DessinStylet {
  traits: Trait[]
  outil: 'stylo' | 'gomme'
  setOutil: (o: 'stylo' | 'gomme') => void
  peutAnnuler: boolean
  peutRefaire: boolean
  annuler: () => void
  refaire: () => void
  /** Remplace `traits` ET vide l'historique — chargement d'une page, ou reset
   * du canvas de dictée après une validation. */
  charger: (traits: Trait[]) => void
  canvasRef: React.RefObject<HTMLCanvasElement | null>
  zoneRef: React.RefObject<HTMLDivElement | null>
  onPointerDown: (e: React.PointerEvent<HTMLCanvasElement>) => void
  onPointerMove: (e: React.PointerEvent<HTMLCanvasElement>) => void
  /** Même gestionnaire pour `onPointerUp`/`onPointerCancel`/`onPointerLeave` :
   * les trois signifient « ce contact est terminé, quelle qu'en soit la
   * raison ». */
  terminer: (e: React.PointerEvent<HTMLCanvasElement>) => void
}

/**
 * Un canvas à stylet complet : traits, outil (stylo/gomme), undo/redo, rendu.
 * La PERSISTANCE (autosave, chargement, validation) reste hors de ce hook —
 * c'est ce qui le rend réutilisable par deux flux dont seule la persistance
 * diffère.
 */
export function useDessinStylet(options: OptionsDessinStylet = {}): DessinStylet {
  const largeur = options.largeur ?? LARGEUR
  const hauteur = options.hauteur ?? HAUTEUR

  const [traits, setTraits] = useState<Trait[]>([])
  //: Instantanés de `traits`, pas une pile de traits ajoutés — ce qui rend la
  //: gomme annulable au même titre que l'écriture.
  const [passe, setPasse] = useState<Trait[][]>([])
  const [futur, setFutur] = useState<Trait[][]>([])
  const [outil, setOutil] = useState<'stylo' | 'gomme'>('stylo')

  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const zoneRef = useRef<HTMLDivElement | null>(null)
  //: Le trait en cours de tracé. Dans une ref et NON dans l'état : un stylet
  //: produit plusieurs centaines de points par seconde, et un rendu React par
  //: point rendrait l'écriture inutilisable.
  const enCoursRef = useRef<Trait | null>(null)
  const pointeurRef = useRef<number | null>(null)
  const typeEnCoursRef = useRef<string>('')
  const debutRef = useRef(0)
  //: Un stylet est-il en contact ? Tant que oui, tout événement tactile est
  //: ignoré — c'est la paume posée sur l'écran.
  const styletActifRef = useRef(false)
  const trameRef = useRef(0)

  const appliquer = useCallback((suivant: Trait[]) => {
    setPasse(p => [...p, traits].slice(-PROFONDEUR_HISTORIQUE))
    setFutur([])
    setTraits(suivant)
  }, [traits])

  // `annuler`/`refaire` lisent `passe`/`futur` par la forme FONCTIONNELLE de
  // `setPasse`/`setFutur` (`p => …`) plutôt que par fermeture directe : c'est
  // ce qui permet à ces callbacks de rester stables (`useCallback([traits])`,
  // sans `passe`/`futur` en dépendance) tout en lisant leur valeur la plus
  // FRAÎCHE — une fermeture directe sur `passe`/`futur` obligerait à les
  // ajouter aux dépendances et recréerait la fonction à chaque annulation.
  const annuler = useCallback(() => {
    setPasse(p => {
      if (p.length === 0) return p
      setFutur(f => [traits, ...f])
      setTraits(p[p.length - 1])
      return p.slice(0, -1)
    })
  }, [traits])

  const refaire = useCallback(() => {
    setFutur(f => {
      if (f.length === 0) return f
      setPasse(p => [...p, traits].slice(-PROFONDEUR_HISTORIQUE))
      setTraits(f[0])
      return f.slice(1)
    })
  }, [traits])

  const charger = useCallback((nouveaux: Trait[]) => {
    setTraits(nouveaux)
    setPasse([])
    setFutur([])
  }, [])

  // ── Dimensionnement et rendu ──────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current
    const zone = zoneRef.current
    if (!canvas || !zone) return
    const ajuster = () => {
      if (dimensionner(canvas, zone, largeur, hauteur)) {
        peindre(canvas, largeur, hauteur, traits, enCoursRef.current)
      }
    }
    ajuster()
    // jsdom n'implémente pas ResizeObserver : l'appelant en pose un bouchon
    // dans ses tests s'il veut éprouver ce chemin. Le garde ici couvre aussi
    // un navigateur trop ancien, où la page reste figée à sa taille initiale.
    if (typeof ResizeObserver === 'undefined') return
    const observateur = new ResizeObserver(ajuster)
    observateur.observe(zone)
    return () => observateur.disconnect()
  }, [traits, largeur, hauteur])

  const repeindre = useCallback(() => {
    if (trameRef.current) return
    trameRef.current = requestAnimationFrame(() => {
      trameRef.current = 0
      const canvas = canvasRef.current
      if (canvas) peindre(canvas, largeur, hauteur, traits, enCoursRef.current)
    })
  }, [traits, largeur, hauteur])

  const abandonner = useCallback(() => {
    enCoursRef.current = null
    pointeurRef.current = null
    typeEnCoursRef.current = ''
    repeindre()
  }, [repeindre])

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    const natif = e.nativeEvent
    if (natif.pointerType === 'touch' && styletActifRef.current) return
    if (natif.pointerType === 'pen') {
      styletActifRef.current = true
      // La paume touche l'écran AVANT la pointe : le trait qu'elle a commencé
      // est abandonné, pas seulement interrompu. Sans ça, la règle « ignorer
      // le tactile pendant qu'un stylet écrit » arrive toujours trop tard.
      if (typeEnCoursRef.current === 'touch') abandonner()
    }
    const canvas = canvasRef.current
    if (!canvas) return
    const { x, y } = versLogique(canvas, natif, largeur, hauteur)

    if (outil === 'gomme') {
      pointeurRef.current = natif.pointerId
      typeEnCoursRef.current = natif.pointerType
      canvas.setPointerCapture(natif.pointerId)
      setTraits(actuels => {
        const restants = actuels.filter(t => !souLaGomme(t, x, y))
        if (restants.length === actuels.length) return actuels
        setPasse(p => [...p, actuels].slice(-PROFONDEUR_HISTORIQUE))
        setFutur([])
        return restants
      })
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
  }, [outil, largeur, hauteur, abandonner, repeindre])

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    const natif = e.nativeEvent
    if (pointeurRef.current !== natif.pointerId) return
    const canvas = canvasRef.current
    if (!canvas) return

    // LE point du stylet : un `pointermove` en regroupe plusieurs. Sans cet
    // appel, la moitié des points d'un stylet à 240 Hz est perdue et les
    // courbes rapides deviennent des segments.
    const bruts = typeof natif.getCoalescedEvents === 'function'
      ? natif.getCoalescedEvents()
      : [natif]
    const echantillons = bruts.length > 0 ? bruts : [natif]

    if (outil === 'gomme') {
      setTraits(actuels => {
        let restants = actuels
        for (const ev of echantillons) {
          const { x, y } = versLogique(canvas, ev, largeur, hauteur)
          restants = restants.filter(t => !souLaGomme(t, x, y))
        }
        if (restants.length === actuels.length) return actuels
        setPasse(p => [...p, actuels].slice(-PROFONDEUR_HISTORIQUE))
        setFutur([])
        return restants
      })
      return
    }

    const enCours = enCoursRef.current
    if (!enCours) return
    for (const ev of echantillons) {
      const { x, y } = versLogique(canvas, ev, largeur, hauteur)
      enCours.points.push({
        x, y,
        pression: ev.pressure > 0 ? ev.pressure : 0.5,
        t: Math.round(ev.timeStamp - debutRef.current),
      })
    }
    repeindre()
  }, [outil, largeur, hauteur, repeindre])

  const terminer = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    const natif = e.nativeEvent
    if (natif.pointerType === 'pen') styletActifRef.current = false
    if (pointeurRef.current !== natif.pointerId) return
    const enCours = enCoursRef.current
    // La ref est vidée AVANT `appliquer` : sinon l'effet de rendu, déclenché
    // par le changement de `traits`, dessinerait le trait deux fois.
    enCoursRef.current = null
    pointeurRef.current = null
    typeEnCoursRef.current = ''
    if (enCours && enCours.points.length > 0) appliquer([...traits, enCours])
    else repeindre()
  }, [appliquer, traits, repeindre])

  return {
    traits, outil, setOutil,
    peutAnnuler: passe.length > 0, peutRefaire: futur.length > 0,
    annuler, refaire, charger,
    canvasRef, zoneRef,
    onPointerDown, onPointerMove, terminer,
  }
}
