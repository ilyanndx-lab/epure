import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/**
 * Tests de composants — le premier harnais frontend du dépôt.
 *
 * Pourquoi il arrive maintenant : un `TypeError: Cannot read properties of
 * undefined (reading 'length')` en production, dans un chunk minifié
 * (`ModuleBar-<hash>.js:3:5151`), sur un rendu de `ModuleBar`. Le type-check ne
 * pouvait pas le voir — le bug vient précisément d'un `as` posé sur un
 * `r.json()`, c'est-à-dire d'une affirmation de forme que TypeScript croit sur
 * parole. Il faut donc RENDRE le composant, avec des réponses qui n'ont pas la
 * forme annoncée.
 *
 * Config séparée de `vite.config.ts` plutôt qu'un bloc `test:` dedans : le
 * fichier de build porte l'alias de neutralisation de l'Atelier et un `define`
 * daté, qui ne concernent en rien les tests. Le plugin react, lui, est
 * indispensable — sans lui le JSX n'est pas transformé.
 *
 * `environment: 'jsdom'` : `src/api.ts` lit `localStorage` à l'import et
 * `window.location` pour l'URL WebSocket. Sans DOM, l'import échoue avant le
 * premier test.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    // Chaque test repose son propre `fetch` : sans restauration, l'ordre
    // d'exécution deviendrait significatif.
    restoreMocks: true,
  },
})
