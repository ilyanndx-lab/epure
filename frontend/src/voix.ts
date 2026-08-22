import { useSyncExternalStore } from 'react'

import { API, apiFetch } from './api'

/**
 * Disponibilité des capacités vocales, pour MASQUER les contrôles plutôt que les
 * offrir à un clic qui échoue.
 *
 * Même traitement que les fournisseurs cloud sans clé configurée : on ne propose
 * pas un bouton dont on sait qu'il rendra une erreur. Le cas qui rend ça
 * nécessaire n'est pas hypothétique — sur Windows ARM64 la voix est déclarée
 * indisponible (décision du 2026-08-22, `docs/remplacement-vectoriel.md`) :
 * `faster-whisper` et `piper-tts` ne sont pas installés du tout, faute de wheel
 * `win_arm64`, et un micro affiché n'y produit qu'un 503 à chaque appui.
 *
 * Source : `GET /voice/capabilities`, qui ne fait que regarder si les paquets sont
 * présents (`find_spec`, sans les importer). Endpoint distinct de `/models` — où
 * vit la disponibilité des fournisseurs cloud — parce que celui-là interroge
 * quatre API distantes : faire dépendre l'affichage d'un bouton micro d'un
 * aller-retour réseau serait payer le réseau pour une réponse qui est sur le
 * disque local.
 *
 * Question distincte de `GET /voice/model`, que `App.tsx` interroge par ailleurs :
 * celui-là dit si le modèle de 76 Mo est là (récupérable en cliquant), celui-ci si
 * le code capable de le lire existe (définitif).
 */

const CACHE_KEY = 'epure.voix'

export interface CapaciteVocale {
  disponible: boolean
  manquants: string[]
  raison: string
}

/** État exposé aux composants, en champs ASCII pour des sites d'appel lisibles. */
export interface EtatVoix {
  /** Micro / dictée (faster-whisper + ctranslate2). */
  transcription: boolean
  /** Lecture à voix haute (piper-tts). */
  synthese: boolean
  /** Ce qui manque, pour l'expliquer si un jour on l'affiche quelque part. */
  raisons: { transcription: string; synthese: string }
}

/**
 * État de départ : les deux capacités MASQUÉES, et c'est un choix, pas un défaut
 * par paresse. L'inverse (optimiste) ferait apparaître un micro puis le retirerait
 * une fois la réponse arrivée — sur la machine où il ne marche pas, donc
 * exactement là où il ne faut pas le montrer. Masquer d'abord ne coûte qu'un
 * bouton qui apparaît quelques millisecondes plus tard, et le cache
 * `localStorage` supprime même ce délai dès la deuxième ouverture (même idiome
 * que `instance.ts`).
 */
const INCONNU: EtatVoix = {
  transcription: false,
  synthese: false,
  raisons: { transcription: '', synthese: '' },
}

const listeners = new Set<() => void>()
let etat: EtatVoix = lireCache() ?? INCONNU
let demandeEnCours = false

function lireCache(): EtatVoix | null {
  try {
    const brut = localStorage.getItem(CACHE_KEY)
    return brut ? { ...INCONNU, ...JSON.parse(brut) } : null
  } catch {
    return null
  }
}

function poser(suivant: EtatVoix) {
  etat = suivant
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(suivant))
  } catch {
    /* quota plein — sans gravité */
  }
  listeners.forEach(l => l())
}

/**
 * Va chercher le verdict, une seule fois par session.
 *
 * En cas d'échec réseau ou de route absente (backend plus ancien), on ACTIVE les
 * deux capacités : seul un « non » explicite du serveur masque un contrôle. Une
 * incertitude ne doit pas retirer la voix à quelqu'un qui l'a — c'est la même
 * règle que `confirmerModeleVocal` dans `App.tsx`, et elle penche du bon côté :
 * au pire un bouton rend une erreur qu'on sait déjà présenter proprement (503),
 * au mieux on n'a rien cassé chez ceux dont le backend ne connaît pas la route.
 */
export async function chargerVoix(): Promise<void> {
  if (demandeEnCours) return
  demandeEnCours = true
  const tout = (raison: string): EtatVoix => ({
    transcription: true, synthese: true,
    raisons: { transcription: raison, synthese: raison },
  })
  try {
    const res = await apiFetch(`${API}/voice/capabilities`)
    if (!res.ok) {
      poser(tout(''))
      return
    }
    const corps = await res.json() as Record<string, CapaciteVocale>
    const t = corps['transcription']
    const s = corps['synthèse']
    poser({
      transcription: t?.disponible ?? true,
      synthese: s?.disponible ?? true,
      raisons: { transcription: t?.raison ?? '', synthese: s?.raison ?? '' },
    })
  } catch {
    poser(tout(''))
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  // Premier abonné : c'est le moment de demander. Rien n'est chargé sur un écran
  // qui ne parle pas de voix.
  if (!demandeEnCours) void chargerVoix()
  return () => { listeners.delete(listener) }
}

function getSnapshot(): EtatVoix {
  return etat
}

/** Capacités vocales réactives, partagées par tous les composants. */
export function useVoix(): EtatVoix {
  return useSyncExternalStore(subscribe, getSnapshot)
}
