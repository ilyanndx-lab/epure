/**
 * Base des appels HTTP (`src/api.ts`) : relative dans un build, `VITE_API_URL`
 * seulement en dev.
 *
 * Relevé pendant la preuve de la chaîne CSP (#72, Edge 155 + CDP) : le build
 * construit par `tools/dev-epure.ps1` (sans `VITE_API_URL`) et ouvert par
 * `http://127.0.0.1:8000/` appelait `http://localhost:8000` — autre origine,
 * 14 violations de `connect-src 'self'`. Un build est toujours servi par le
 * backend lui-même : la même origine est la seule base juste, quel que soit
 * l'hôte tapé dans la barre d'adresse.
 */
import { describe, expect, it } from 'vitest'
import { baseApi } from './api'

describe('baseApi', () => {
  it('build : base relative, quelle que soit VITE_API_URL', () => {
    expect(baseApi(false, undefined)).toBe('')
    expect(baseApi(false, '/')).toBe('')
    expect(baseApi(false, 'http://localhost:8000')).toBe('')
  })

  it('dev : backend :8000 par défaut, VITE_API_URL sinon', () => {
    expect(baseApi(true, undefined)).toBe('http://localhost:8000')
    expect(baseApi(true, 'http://127.0.0.1:8000/')).toBe('http://127.0.0.1:8000')
    expect(baseApi(true, '/')).toBe('')
  })
})
