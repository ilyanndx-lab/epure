import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'

/**
 * Comme useState, mais la valeur survit à un rechargement de page (F5, ou le
 * reload complet déclenché par Vite quand l'atelier ajoute un module généré).
 *
 * - Persistance dans localStorage sous `key`, debouncée (évite d'écrire à chaque
 *   frappe/token pendant un streaming).
 * - Flush IMMÉDIAT avant tout déchargement (pagehide/beforeunload) : un reload
 *   qui survient pendant la fenêtre de debounce ne perd donc pas la valeur.
 * - Lecture au montage : valeur stockée si présente, sinon `initial`.
 * - Échecs (quota, JSON invalide, mode privé) ignorés silencieusement.
 */
export function usePersistentState<T>(
  key: string,
  initial: T | (() => T),
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key)
      if (raw != null) return JSON.parse(raw) as T
    } catch { /* ignore */ }
    return typeof initial === 'function' ? (initial as () => T)() : initial
  })

  const latest = useRef(value)
  const keyRef = useRef(key)

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
    const id = setTimeout(() => {
      try {
        localStorage.setItem(key, JSON.stringify(value))
      } catch { /* quota / privé : tant pis */ }
    }, 400)
    return () => clearTimeout(id)
  }, [key, value])

  // Filet de sécurité : flush synchrone avant un reload/fermeture, pour ne pas
  // perdre une valeur encore dans la fenêtre de debounce (cas du reload Vite).
  useEffect(() => {
    const flush = () => {
      try {
        localStorage.setItem(keyRef.current, JSON.stringify(latest.current))
      } catch { /* ignore */ }
    }
    window.addEventListener('pagehide', flush)
    window.addEventListener('beforeunload', flush)
    return () => {
      window.removeEventListener('pagehide', flush)
      window.removeEventListener('beforeunload', flush)
    }
  }, [])

  return [value, setValue]
}
