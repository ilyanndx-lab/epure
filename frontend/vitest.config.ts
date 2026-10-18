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
 *
 * `testTimeout: 20_000` (défaut vitest : 5 s) — issue #68. Les tests de
 * composants du chat et des Réglages montent l'écran ENTIER et l'interrogent
 * par `getAllByRole`, qui recalcule l'arbre d'accessibilité : du CPU pur, qui
 * s'étire avec la charge de la machine. Mesuré le 2026-10-18 (Windows,
 * 12 cœurs) : le premier test de `Component.comparaison.test.tsx` prend
 * 1,5 s machine au repos, 0,4 s quand son fichier tourne presque seul — et
 * dépasse 5 s dès que la machine est chargée (reproduit à coup sûr en lançant
 * deux suites en parallèle : 1 échec par suite). Le premier test d'un
 * fichier est le plus touché (≈ 2,5× les suivants : premier rendu complet).
 * Ces tests sont déterministes (WebSocket factice, `fetch` stubé) : rien n'y
 * attend le réseau ni une horloge, donc 5 s ne mesurait pas un blocage, juste
 * la vitesse du poste. Avec 20 s, trois suites en parallèle passent 275/275 ;
 * le test le plus lent y prend 8,9 s (2,3 s au repos) — marge ×2,2 sous
 * charge triple. Un vrai blocage échoue toujours, 15 s plus tard.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    // Chaque test repose son propre `fetch` : sans restauration, l'ordre
    // d'exécution deviendrait significatif.
    restoreMocks: true,
    testTimeout: 20_000,
    execArgv: ['--no-experimental-webstorage'],
  },
})
