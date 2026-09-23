/**
 * Redémarrage du backend après un changement de modules — logique hors rendu.
 *
 * Pourquoi : les modules ne se montent plus à chaud (backend
 * `core/module_registry.py`, en-tête). Installer, approuver, supprimer, activer
 * ou désactiver un module change l'état VOULU ; le backend ne le charge qu'à
 * son redémarrage. `GET /instance/redemarrage` dit s'il en faut un — c'est un
 * ÉCART calculé côté serveur, pas un drapeau : l'interface ne le stocke pas,
 * elle le relit.
 *
 * Trois choses vivent ici plutôt que dans `components/RedemarrageRequis.tsx` :
 *  - la normalisation des réponses (une réponse d'erreur n'a pas la forme
 *    annoncée — cf. `ModuleBar.test.tsx`, piège déjà payé) ;
 *  - le registre des flux en cours, que le chat et l'Atelier alimentent, pour
 *    PRÉVENIR avant de couper une génération ;
 *  - l'attente du retour : `/health` porte le `boot_id` du processus, et c'est
 *    son CHANGEMENT qui prouve le redémarrage — l'ancien processus répond
 *    encore pendant les ~2 s de sondage du tray.
 * Un fichier de composant qui exporterait aussi ces fonctions casserait le
 * rafraîchissement rapide de Vite (règle eslint `only-export-components`).
 */
import { useEffect } from 'react'
import { API, apiFetch } from './api'

export type Changement = 'à charger' | 'à décharger' | 'modifié'

export interface EtatRedemarrage {
  requis: boolean
  écarts: { id: string; changement: Changement }[]
  automatique: boolean
  boot_id: string | null
}

export interface ReponseDemande {
  déclenché: boolean
  automatique: boolean
  boot_id: string | null
  message?: string
}

const CHANGEMENTS: readonly string[] = ['à charger', 'à décharger', 'modifié']

/** Forme garantie, quoi que le backend ait répondu. `requis` n'est vrai que si
 *  le backend l'a DIT : un 500 ou un corps inattendu n'affiche rien. */
export function normaliserEtat(brut: unknown): EtatRedemarrage {
  const o = (brut && typeof brut === 'object' ? brut : {}) as Record<string, unknown>
  const ecarts = Array.isArray(o['écarts'])
    ? (o['écarts'] as unknown[]).flatMap(e => {
        const x = (e && typeof e === 'object' ? e : {}) as Record<string, unknown>
        return typeof x.id === 'string' && typeof x.changement === 'string'
          && CHANGEMENTS.includes(x.changement)
          ? [{ id: x.id, changement: x.changement as Changement }]
          : []
      })
    : []
  return {
    requis: o.requis === true,
    écarts: ecarts,
    automatique: o.automatique === true,
    boot_id: typeof o.boot_id === 'string' ? o.boot_id : null,
  }
}

export async function lireEtat(): Promise<EtatRedemarrage> {
  try {
    const res = await apiFetch(`${API}/instance/redemarrage`)
    if (!res.ok) return normaliserEtat(null)
    return normaliserEtat(await res.json())
  } catch {
    return normaliserEtat(null)
  }
}

export async function demanderRedemarrage(raison: string): Promise<ReponseDemande> {
  try {
    const res = await apiFetch(`${API}/instance/redemarrage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ raison }),
    })
    const o = (await res.json().catch(() => ({}))) as Record<string, unknown>
    if (!res.ok) {
      return { déclenché: false, automatique: false, boot_id: null,
               message: `Le backend a refusé la demande (HTTP ${res.status}).` }
    }
    return {
      déclenché: o['déclenché'] === true,
      automatique: o.automatique === true,
      boot_id: typeof o.boot_id === 'string' ? o.boot_id : null,
      message: typeof o.message === 'string' ? o.message : undefined,
    }
  } catch {
    return { déclenché: false, automatique: false, boot_id: null,
             message: 'Backend injoignable : la demande de redémarrage n\'est pas partie.' }
  }
}

/** `boot_id` du backend qui répond, ou null (injoignable, ou réponse sans lui). */
export async function lireBootId(): Promise<string | null> {
  try {
    const res = await apiFetch(`${API}/health`)
    if (!res.ok) return null
    const o = (await res.json()) as Record<string, unknown>
    return typeof o.boot_id === 'string' ? o.boot_id : null
  } catch {
    return null
  }
}

/**
 * Attend qu'un AUTRE processus réponde. `true` s'il est revenu avant `limiteMs`.
 * Mesuré sur ce poste : 6 s sans `--reload`, 10 s avec — la borne par défaut
 * (60 s) laisse de la marge à un premier import lent sans figer l'écran.
 */
export async function attendreRetour(
  ancien: string | null,
  { limiteMs = 60_000, pasMs = 1_000, annule = () => false }:
    { limiteMs?: number; pasMs?: number; annule?: () => boolean } = {},
): Promise<boolean> {
  const fin = Date.now() + limiteMs
  while (Date.now() < fin && !annule()) {
    const actuel = await lireBootId()
    if (actuel !== null && actuel !== ancien) return true
    await new Promise(r => setTimeout(r, pasMs))
  }
  return false
}

// ── Changements de modules : prévenir le bandeau ────────────────────────────

const EVENEMENT = 'epure:modules-modifies'

/** À appeler après une installation, une suppression ou une approbation. */
export function signalerChangementModules(): void {
  window.dispatchEvent(new Event(EVENEMENT))
}

export function surChangementModules(f: () => void): () => void {
  window.addEventListener(EVENEMENT, f)
  return () => window.removeEventListener(EVENEMENT, f)
}

// ── Flux en cours : prévenir avant de couper ────────────────────────────────

const flux = new Map<string, number>()

/**
 * Déclare un flux vivant tant que `actif` est vrai (génération du chat, de
 * l'Atelier). Compteur et non booléen : deux panneaux du même composant
 * (comparaison de modèles) ne doivent pas s'effacer l'un l'autre.
 */
export function useFluxEnCours(nom: string, actif: boolean): void {
  useEffect(() => {
    if (!actif) return
    flux.set(nom, (flux.get(nom) ?? 0) + 1)
    return () => {
      const n = (flux.get(nom) ?? 1) - 1
      if (n <= 0) flux.delete(nom)
      else flux.set(nom, n)
    }
  }, [nom, actif])
}

/** Noms des flux vivants dans CET onglet. Un autre onglet n'est pas vu. */
export function fluxEnCours(): string[] {
  return [...flux.keys()]
}
