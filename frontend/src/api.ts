/**
 * Point d'accès unique au backend Épure.
 *
 * - `API` : URL de base HTTP (cf. les deux modes ci-dessous).
 * - `apiFetch` : fetch qui joint le token d'API (Authorization: Bearer) et
 *   ré-appaire automatiquement sur 401.
 * - `wsUrl('/ws/chat')` : URL WebSocket avec `?token=` (pas de headers possibles
 *   sur `new WebSocket()`).
 *
 * Appairage : le backend exige un token (généré à son premier démarrage) sur
 * toutes les routes sauf /health et /pair. GET /pair renvoie ce token mais
 * uniquement à la machine hôte (localhost) → cas nominal : appairage
 * automatique et invisible. Depuis un autre poste, coller le code affiché par
 * http://localhost:8000/pair ouvert sur la machine qui héberge Épure
 * (via `setToken`, cf. écran d'appairage dans App.tsx).
 *
 * ── Deux modes de déploiement ─────────────────────────────────────────────────
 *
 * 1. **Développement** (`import.meta.env.DEV`) — Vite sert le front sur :5173,
 *    uvicorn l'API sur :8000 : deux origines, donc URL absolue,
 *    `http://localhost:8000` par défaut ou `VITE_API_URL`.
 * 2. **Tout build** (`npm run build` : paquet, `tools/dev-epure.ps1`) — le
 *    front construit est servi par FastAPI lui-même, une seule origine. `API`
 *    est alors TOUJOURS vide (appels relatifs) et `VITE_API_URL` ignorée.
 *    Mesuré le 2026-10-18 (#72, Edge + CDP) : le build de `dev-epure.ps1`,
 *    construit sans `VITE_API_URL` et ouvert par `http://127.0.0.1:8000/`,
 *    appelait `http://localhost:8000` — autre origine, 14 violations de
 *    `connect-src 'self'` (core/csp.py), et un appel cross-origin que
 *    EPURE_CORS_ORIGINS (origines :5173) ne couvre pas. Seule la même origine
 *    est juste, quel que soit l'hôte tapé dans la barre d'adresse.
 *
 * En dev, `undefined` et la chaîne vide restent distincts : `VITE_API_URL=/`
 * (ou `''` en POSIX — sous Windows `$env:VAR = ''` SUPPRIME la variable,
 * mesuré) donne une base relative, utile derrière un proxy. `tools/faire_paquet.py`
 * pose encore `VITE_API_URL=/` au build : sans effet désormais, inoffensif.
 */

/** Base HTTP : `''` (même origine) dans un build ; en dev, `VITE_API_URL`
 * ou le backend :8000. Fonction pure, exportée pour `api.base.test.ts`. */
export function baseApi(dev: boolean, url: string | undefined): string {
  if (!dev) return ''
  return url === undefined ? 'http://localhost:8000' : url.replace(/\/+$/, '')
}

export const API: string = baseApi(
  import.meta.env.DEV, import.meta.env.VITE_API_URL as string | undefined)

/**
 * Dans un build, `API` est vide : on ne peut pas en dériver le schéma ws. On le
 * prend sur la page, qui EST le backend. Résolu explicitement plutôt que de
 * confier `/ws/chat` au constructeur WebSocket : la résolution d'une URL
 * relative y est bien spécifiée, mais un `ws://` complet reste lisible dans les
 * outils réseau et dans les logs uvicorn.
 */
const WS_BASE = API
  ? API.replace(/^http/, 'ws')
  : window.location.origin.replace(/^http/, 'ws')

const TOKEN_KEY = 'epure.apiToken'

let token: string | null = localStorage.getItem(TOKEN_KEY)

export function getToken(): string | null {
  return token
}

export function setToken(t: string) {
  token = t.trim()
  localStorage.setItem(TOKEN_KEY, token)
}

export type PairResult = 'ok' | 'forbidden' | 'unreachable'

/** Tente l'appairage automatique (ne fonctionne que depuis la machine hôte). */
export async function pair(): Promise<PairResult> {
  try {
    const res = await fetch(`${API}/pair`)
    if (res.ok) {
      const data = await res.json()
      if (data?.token) {
        setToken(data.token)
        return 'ok'
      }
    }
    return res.status === 403 ? 'forbidden' : 'unreachable'
  } catch {
    return 'unreachable' // backend éteint : l'UX « backend injoignable » existante s'applique
  }
}

/** Garantit un token si possible ; 'forbidden' → afficher l'écran d'appairage. */
export async function ensureToken(): Promise<PairResult> {
  return token ? 'ok' : pair()
}

function withAuth(init: RequestInit): RequestInit {
  return {
    ...init,
    headers: {
      ...(init.headers || {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  }
}

/**
 * fetch + token d'API. `input` : URL complète (`${API}/...`) comme les appels
 * existants. Sur 401 (token absent/périmé), ré-appaire une fois puis rejoue.
 */
export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  if (!token) await pair()
  let res = await fetch(input, withAuth(init))
  if (res.status === 401 && (await pair()) === 'ok') {
    res = await fetch(input, withAuth(init))
  }
  return res
}

/** URL WebSocket authentifiée : wsUrl('/ws/chat') → ws://…/ws/chat?token=… */
export function wsUrl(path: string): string {
  const sep = path.includes('?') ? '&' : '?'
  return `${WS_BASE}${path}${token ? `${sep}token=${encodeURIComponent(token)}` : ''}`
}
