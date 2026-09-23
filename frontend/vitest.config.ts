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
 *
 * `--no-experimental-webstorage` : depuis Node 25, Node expose son PROPRE
 * `localStorage` global (Web Storage activé par défaut). Sans
 * `--localstorage-file` c'est un objet sans méthodes, et il masque celui de
 * jsdom : `TypeError: localStorage.getItem is not a function` à l'import de
 * `src/api.ts`, 13 fichiers de test sur 17 en échec. On coupe la fonction à
 * la source, dans le process des workers, plutôt que de réaffecter le global
 * dans un setup : l'environnement jsdom s'installe alors sans concurrent,
 * quelle que soit la façon dont vitest peuple les globaux. Le drapeau est
 * accepté de Node 22 à 25 (vérifié), sans effet là où la fonction est déjà
 * désactivée. Node est par ailleurs épinglé par `.nvmrc` ; ceci protège un
 * poste qui ne le respecte pas. Verrouillé par `src/webstorage.test.ts`.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    // Chaque test repose son propre `fetch` : sans restauration, l'ordre
    // d'exécution deviendrait significatif.
    restoreMocks: true,
    execArgv: ['--no-experimental-webstorage'],
  },
})
