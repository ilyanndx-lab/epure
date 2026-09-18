import type { EffortLevel } from './App'

/**
 * Libellés d'affichage des niveaux d'effort.
 *
 * Extraits de `ModuleBar.tsx` (qui les définissait localement) dans leur propre
 * fichier pour que `modules/chat/Component.tsx` puisse les réutiliser dans son
 * popover « Paramètres de la conversation » sans faire de `ModuleBar.tsx` un
 * module mixte (composant + constante) : `react-refresh/only-export-components`
 * l'interdit en ERREUR, pas en avertissement — même raison que `commands.ts`
 * (cf. sa docstring).
 */
export const EFFORT_LABELS: Record<EffortLevel, string> = {
  direct: 'Direct',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  adaptive: 'Adaptatif',
}
