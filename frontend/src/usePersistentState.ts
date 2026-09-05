import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'

/**
 * État de la dernière tentative d'écriture dans `localStorage`.
 *
 * `ok: false` veut dire une seule chose, quelle que soit la cause : **la valeur
 * n'est PAS conservée**. Un `QuotaExceededError` (stockage plein) et un refus
 * en navigation privée ont le même sens pour l'appelant, donc aucune branche ne
 * doit dépendre de `erreur` — qui n'est là que pour le diagnostic.
 */
export interface EtatPersistance {
  ok: boolean
  /** Nom de l'erreur (`QuotaExceededError`, …), `null` si l'écriture a réussi. */
  erreur: string | null
  /**
   * Date à laquelle cet état a été ATTEINT — pas celle de la dernière
   * tentative : des échecs répétés à l'identique gardent l'horodatage du
   * premier, faute de quoi une panne de quota provoquerait un rendu à chaque
   * tick de debounce, sans fin.
   *
   * `0` = aucune écriture observée pour l'instant. C'est le seul discriminant
   * du `ok: true` initial, qui est une valeur par défaut et non un constat :
   * entre le montage et la première écriture debouncée (400 ms), rien n'a
   * encore été tenté.
   */
  horodatage: number
}

const PERSISTANCE_INITIALE: EtatPersistance = { ok: true, erreur: null, horodatage: 0 }

/** Nom lisible de ce qui a été levé — un navigateur peut lever autre chose
 *  qu'une `Error`, et un `.name` lu à l'aveugle donnerait `undefined`. */
function nomErreur(e: unknown): string {
  if (e instanceof Error && e.name) return e.name
  if (typeof e === 'string' && e) return e
  return 'Error'
}

/**
 * Comme useState, mais la valeur survit à un rechargement de page (F5, ou le
 * reload complet déclenché par Vite quand l'atelier ajoute un module généré).
 *
 * - Persistance dans localStorage sous `key`, debouncée (évite d'écrire à chaque
 *   frappe/token pendant un streaming).
 * - Flush IMMÉDIAT avant tout déchargement (pagehide/beforeunload) : un reload
 *   qui survient pendant la fenêtre de debounce ne perd donc pas la valeur.
 * - Lecture au montage : valeur stockée si présente, sinon `initial`.
 * - Une LECTURE ratée (JSON invalide, stockage inaccessible) retombe sur
 *   `initial` sans bruit : il n'y a rien à sauver, et rien que l'appelant
 *   puisse faire.
 * - Une ÉCRITURE ratée est en revanche **rapportée** par le 3e élément du
 *   tuple. Elle l'était autrefois dans un `catch {}` muet — un échec réel,
 *   invisible, de la même famille qu'un `.json()` cru sur parole (§8 de
 *   CLAUDE.md). Ça n'est pas théorique : le module Code fait reposer son garde
 *   `beforeunload` sur l'idée que ses onglets reviendront d'ici, et sur un
 *   quota dépassé l'utilisateur arbitrerait « quitter ou rester ? » avec une
 *   information fausse.
 *
 * **Le 3e élément est optionnel à la lecture** : `const [v, setV] = …` reste
 * valide, et c'est ce qu'écrivent la douzaine de composants déjà branchés
 * dessus. Ne pas transformer ce retour en objet.
 */
export function usePersistentState<T>(
  key: string,
  initial: T | (() => T),
): [T, Dispatch<SetStateAction<T>>, EtatPersistance] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key)
      if (raw != null) return JSON.parse(raw) as T
    } catch { /* ignore */ }
    return typeof initial === 'function' ? (initial as () => T)() : initial
  })

  const [persistance, setPersistance] = useState<EtatPersistance>(PERSISTANCE_INITIALE)

  const latest = useRef(value)
  const keyRef = useRef(key)

  /**
   * Écrit et rapporte. Point d'écriture UNIQUE des deux chemins ci-dessous :
   * l'un des deux resterait muet si chacun avait son `try`.
   *
   * `setPersistance` ne rejoue PAS l'effet d'écriture (il ne dépend que de
   * `key`/`value`), donc pas de boucle. On ne remplace un état identique que
   * s'il change vraiment — un flush qui réussit à chaque `pagehide` ne doit pas
   * provoquer un rendu pour rien.
   */
  const ecrire = useCallback((k: string, v: T) => {
    let etat: EtatPersistance
    try {
      localStorage.setItem(k, JSON.stringify(v))
      etat = { ok: true, erreur: null, horodatage: Date.now() }
    } catch (e) {
      etat = { ok: false, erreur: nomErreur(e), horodatage: Date.now() }
    }
    setPersistance(prev =>
      prev.ok === etat.ok && prev.erreur === etat.erreur ? prev : etat)
  }, [])

  /**
   * Les refs sont rafraîchies dans un EFFET, pas pendant le rendu.
   *
   * `latest.current = value` en plein corps de fonction est ce que la règle
   * `react-hooks/refs` signale (« Cannot update ref during render ») : sous le
   * rendu concurrent, un rendu peut être abandonné, et la ref garderait alors la
   * valeur d'un rendu qui n'a jamais été commité. C'est aussi ce qui faisait
   * passer le cliquet eslint de 61 à 63 avertissements et bloquait la CI.
   *
   * Personne ne lit ces refs PENDANT un rendu : elles servent au seul
   * gestionnaire `flush` ci-dessous, appelé au déchargement de la page, donc
   * toujours après que les effets ont tourné.
   */
  useEffect(() => {
    latest.current = value
    keyRef.current = key
  }, [key, value])

  // Écriture debouncée à chaque changement de valeur.
  //
  // Lit `value` directement plutôt que `latest.current` : cet effet se rejoue à
  // chaque changement de valeur (il en dépend), donc la fermeture est toujours
  // fraîche. La ref n'y a jamais rien apporté.
  useEffect(() => {
    const id = setTimeout(() => ecrire(key, value), 400)
    return () => clearTimeout(id)
  }, [key, value, ecrire])

  // Filet de sécurité : flush synchrone avant un reload/fermeture, pour ne pas
  // perdre une valeur encore dans la fenêtre de debounce (cas du reload Vite).
  //
  // L'échec y est rapporté comme ailleurs, et ce n'est pas inutile malgré le
  // déchargement en cours : un `beforeunload` peut être ANNULÉ — c'est même
  // exactement ce que fait le garde du module Code quand un onglet est sale.
  // La page continue alors de vivre, et doit dire la vérité sur ce qui a été
  // conservé.
  useEffect(() => {
    const flush = () => ecrire(keyRef.current, latest.current)
    window.addEventListener('pagehide', flush)
    window.addEventListener('beforeunload', flush)
    return () => {
      window.removeEventListener('pagehide', flush)
      window.removeEventListener('beforeunload', flush)
    }
  }, [ecrire])

  return [value, setValue, persistance]
}
