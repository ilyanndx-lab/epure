/**
 * Point d'accès unique au backend Épure.
 *
 * - `API` : URL de base HTTP (surchargeable par VITE_API_URL, défaut localhost:8000).
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
 */

export const API: string =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/+$/, '') ||
  'http://localhost:8000'

const WS_BASE = API.replace(/^http/, 'ws')

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
