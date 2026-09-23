import { describe, expect, it } from 'vitest'

/**
 * Le `localStorage` vu par les tests est celui de jsdom — pas celui de Node.
 *
 * Depuis Node 25, Node expose son propre `localStorage` global, sans méthodes
 * faute de `--localstorage-file`, et il masquait celui de jsdom : 13 fichiers
 * de test sur 17 tombaient sur `localStorage.getItem is not a function` à
 * l'import de `src/api.ts`. `vitest.config.ts` passe
 * `--no-experimental-webstorage` aux workers.
 *
 * Le premier cas vérifie le BRANCHEMENT et échoue sur n'importe quelle version
 * de Node si le drapeau disparaît de la config — la CI tourne sur Node 24, où
 * le second cas passerait même sans correctif.
 */
// `process` sans `@types/node` : tsconfig.app.json ne charge que `vite/client`,
// et une dépendance de plus pour une seule ligne ne se justifie pas.
const { execArgv } = (globalThis as unknown as { process: { execArgv: string[] } }).process

describe('localStorage des tests', () => {
  it('les workers tournent avec --no-experimental-webstorage', () => {
    expect(execArgv).toContain('--no-experimental-webstorage')
  })

  it('est un Storage utilisable (aller-retour)', () => {
    localStorage.setItem('epure.test.webstorage', 'ok')
    expect(localStorage.getItem('epure.test.webstorage')).toBe('ok')
    localStorage.removeItem('epure.test.webstorage')
  })
})
