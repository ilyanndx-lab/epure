import { useSyncExternalStore } from 'react'

import { API, apiFetch } from './api'

/**
 * État de préparation du moteur de recherche documentaire, pour EXPLIQUER au lieu
 * d'afficher une erreur.
 *
 * L'INCIDENT. Dans un paquet livré, le moteur de recherche documentaire n'est pas
 * prêt au premier lancement, et le premier accès produisait
 * `500 {"detail": "Erreur interne du serveur", "type": "ImportError"}` : le
 * panneau fichiers du module Docs était mort d'avance, définitivement, puisque
 * rien dans l'application ne pouvait réparer ce qui manquait
 * (`docs/distribution-empaquetee.md`, « écarts 2 et 3 »).
 *
 * Le backend prépare désormais son moteur lui-même
 * (`backend/core/embedding_install.py`) et répond 503 avec un état pendant ce
 * temps. Ce module lit cet état pour que le panneau dise « préparation en
 * cours, une à deux minutes, connexion réseau nécessaire » plutôt que de rester
 * vide sans raison — et qu'il se remplisse tout seul quand c'est prêt.
 *
 * CE QUI SE PRÉPARE A CHANGÉ le 2026-08-26, et le volume avec : c'était
 * `pip install torch` + `sentence-transformers`, ~2 Go ; ce sont maintenant les
 * 90 Mo de poids d'un modèle ONNX, téléchargés et vérifiés par sha256. Rien à
 * changer ici : le contrat de `GET /rag/capabilities` — les clés, les quatre
 * états, la cause — est resté le même, et c'est exactement ce qu'on lui
 * demandait. Le message et `taille_estimée_mo` viennent du backend, donc
 * l'interface annonce le bon chiffre sans le connaître.
 *
 * Frère de `voix.ts`, avec UNE différence qui change tout le reste : une capacité
 * vocale absente est définitive (aucune wheel `win_arm64` pour `ctranslate2`), on
 * MASQUE le contrôle. Ici l'absence est temporaire — c'est une installation qui
 * n'a pas fini. On n'a donc rien à masquer : on annonce, et on attend en
 * interrogeant.
 *
 * Source : `GET /rag/capabilities`, qui ne fait que regarder le disque
 * (`find_spec` + un fichier d'état) et **ne déclenche rien** — indispensable
 * puisque cette route est appelée en boucle ci-dessous. Le déclenchement, lui,
 * appartient aux routes qui ont réellement besoin du moteur.
 */

/** État du modèle vision, même forme que celle rendue par `GET /rag/capabilities`. */
export interface EtatVision {
  disponible: boolean
  modele: string | null
  source: 'flm' | 'ollama' | null
}

/** Ce que le backend renvoie, champs tels quels (accentués). */
interface CapacitesRecherche {
  'état'?: string
  disponible?: boolean
  message?: string
  cause?: string
  'taille_estimée_mo'?: number
  vision?: { disponible?: boolean; modele?: string | null; source?: string | null }
}

export type EtatPreparation = 'inconnu' | 'absent' | 'en_cours' | 'prêt' | 'échec'

const VISION_INCONNU: EtatVision = { disponible: false, modele: null, source: null }

/** État exposé aux composants, en champs ASCII pour des sites d'appel lisibles. */
export interface EtatRecherche {
  etat: EtatPreparation
  /** Le moteur répond-il ? `inconnu` compte comme oui — cf. INCONNU ci-dessous. */
  prete: boolean
  /** Phrase à afficher telle quelle. Vide quand il n'y a rien à dire. */
  message: string
  /** `réseau` | `pip` | `pip_absent` | '' — décide si un « Réessayer » a un sens. */
  cause: string
  /** Poids annoncé du téléchargement, en Mo. */
  tailleMo: number
  /** État du modèle vision — même appel réseau, pas une seconde requête. */
  vision: EtatVision
}

/**
 * État de départ : le moteur est réputé PRÊT tant que le serveur n'a pas dit le
 * contraire — l'inverse exact du choix fait dans `voix.ts`, et pour une raison
 * qui n'est pas de la symétrie mal recopiée.
 *
 * Là-bas on masque un micro par défaut, parce qu'un bouton affiché sur une
 * machine sans `piper-tts` ne peut QUE échouer. Ici, afficher « préparation en
 * cours » par défaut mentirait sur le poste d'Ilyann, où la pile est installée et
 * où il n'y a rien à préparer : ce serait un bandeau anxiogène sur une
 * installation parfaitement saine, le temps d'un aller-retour. Et un backend plus
 * ancien, qui ne connaît pas `/rag/capabilities`, doit continuer à se comporter
 * exactement comme avant — donc silence.
 *
 * Le coût de ce choix est borné et connu : sur une instance où la pile manque, le
 * bandeau apparaît quelques centaines de millisecondes plus tard. Le coût de
 * l'inverse serait un bandeau faux chez tout le monde.
 */
const INCONNU: EtatRecherche = {
  etat: 'inconnu', prete: true, message: '', cause: '', tailleMo: 0, vision: VISION_INCONNU,
}

/** Cadence d'interrogation pendant la préparation. */
const PERIODE_MS = 4000

const listeners = new Set<() => void>()
let etat: EtatRecherche = INCONNU
let demandeEnCours = false
let minuterie: ReturnType<typeof setTimeout> | null = null

/** Les deux états transitoires : tant qu'on y est, on re-demande. */
function transitoire(e: EtatPreparation): boolean {
  return e === 'absent' || e === 'en_cours'
}

function poser(suivant: EtatRecherche) {
  etat = suivant
  listeners.forEach(l => l())
  // Pas de `localStorage` ici, contrairement à `voix.ts` : là-bas le verdict est
  // définitif et le cache supprime un clignotement à chaque ouverture. Celui-ci
  // change en cours de route — un état « en cours » mis en cache survivrait à
  // l'installation qu'il décrit et afficherait un bandeau sur un moteur prêt.
  if (transitoire(suivant.etat)) programmer()
  else annuler()
}

function programmer() {
  if (minuterie !== null) return
  minuterie = setTimeout(() => { minuterie = null; void chargerRecherche() }, PERIODE_MS)
}

function annuler() {
  if (minuterie === null) return
  clearTimeout(minuterie)
  minuterie = null
}

function normaliser(corps: unknown): EtatRecherche {
  // `as` serait une affirmation, pas une vérification : un corps d'erreur passe
  // le `.json()` sans avoir aucun de ces champs (CLAUDE.md §8). D'où le
  // repli sur INCONNU dès que l'état n'est pas une valeur qu'on connaît.
  const c = (corps ?? {}) as CapacitesRecherche
  const brut = typeof c['état'] === 'string' ? c['état'] : ''
  const connus: EtatPreparation[] = ['absent', 'en_cours', 'prêt', 'échec']
  if (!connus.includes(brut as EtatPreparation)) return INCONNU
  const e = brut as EtatPreparation
  const v = c.vision ?? {}
  const source = v.source === 'flm' || v.source === 'ollama' ? v.source : null
  return {
    etat: e,
    prete: e === 'prêt',
    message: typeof c.message === 'string' ? c.message : '',
    cause: typeof c.cause === 'string' ? c.cause : '',
    tailleMo: typeof c['taille_estimée_mo'] === 'number' ? c['taille_estimée_mo'] : 0,
    vision: {
      disponible: v.disponible === true,
      modele: typeof v.modele === 'string' ? v.modele : null,
      source,
    },
  }
}

/**
 * Va chercher l'état. Réentrant : un appel pendant qu'un autre vole est ignoré.
 *
 * En cas d'échec réseau ou de route absente (backend plus ancien) : INCONNU, donc
 * silence et moteur réputé prêt. Une incertitude ne doit pas afficher un bandeau
 * d'installation à quelqu'un dont la recherche fonctionne — c'est la même règle
 * que `chargerVoix`, appliquée dans l'autre sens parce que le défaut sûr est
 * l'autre.
 */
export async function chargerRecherche(): Promise<void> {
  if (demandeEnCours) return
  demandeEnCours = true
  try {
    const res = await apiFetch(`${API}/rag/capabilities`)
    poser(res.ok ? normaliser(await res.json()) : INCONNU)
  } catch {
    poser(INCONNU)
  } finally {
    demandeEnCours = false
  }
}

/**
 * Redemande la préparation après un échec — le geste du bouton « Réessayer ».
 *
 * Il existe parce que le backend ne réessaie pas tout seul : une seule tentative
 * automatique par process, sans quoi chaque appel concurrent relancerait le même
 * téléchargement. La cause la plus probable d'un échec étant l'absence de
 * réseau, elle se corrige dehors, et l'utilisateur est le seul à savoir quand.
 */
export async function relancerRecherche(): Promise<void> {
  annuler()
  try {
    const res = await apiFetch(`${API}/rag/install`, { method: 'POST' })
    poser(res.ok ? normaliser(await res.json()) : INCONNU)
  } catch {
    poser(INCONNU)
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  // Premier abonné : c'est le moment de demander. Rien n'est chargé sur un écran
  // qui ne parle pas de documents.
  if (!demandeEnCours && etat.etat === 'inconnu') void chargerRecherche()
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) annuler()
  }
}

function getSnapshot(): EtatRecherche {
  return etat
}

/** État de préparation du moteur documentaire, partagé par tous les composants. */
export function useRecherche(): EtatRecherche {
  return useSyncExternalStore(subscribe, getSnapshot)
}

/**
 * Remet l'état du module à zéro. **Réservé aux tests.**
 *
 * Un store de module survit au démontage des composants — c'est tout son intérêt
 * en production, et c'est ce qui fait fuiter l'état d'un test au suivant dans un
 * même fichier vitest. `voix.ts` n'en a pas besoin : son état ne vient que du
 * cache `localStorage`, que le `afterEach` vide déjà. Celui-ci n'est pas mis en
 * cache (cf. `poser`), donc il faut le dire explicitement.
 */
export function reinitialiserRecherche(): void {
  annuler()
  etat = INCONNU
  demandeEnCours = false
}
