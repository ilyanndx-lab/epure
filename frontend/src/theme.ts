/**
 * Thème — désormais géré par le store d'instance (instance.ts).
 *
 * Ce module ne fait que ré-exporter, pour rétro-compatibilité avec les imports
 * existants (`useTheme`, `setTheme`, `initTheme` depuis '../theme'). Le thème
 * est persisté côté serveur (config d'instance) avec localStorage en cache.
 */

import { initInstance, useTheme, setTheme } from './instance'
import type { Theme } from './instance'

export type { Theme }
export { useTheme, setTheme }

/** À appeler une fois avant le premier render (évite le flash de thème). */
export function initTheme() {
  initInstance()
}
