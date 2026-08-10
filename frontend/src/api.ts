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
 * ── Deux modes de déploiement, un seul réglage ────────────────────────────────
 *
 * 1. **Développement** — `VITE_API_URL` non définie. Vite sert le front sur
 *    :5173, uvicorn l'API sur :8000 : deux origines, donc URL absolue.
 * 2. **Paquet distribué** — le front construit est servi par FastAPI lui-même,
 *    une seule origine. `API` doit alors être vide pour que les appels soient
 *    relatifs, sinon `http://localhost:8000` en dur casse dès que le
 *    destinataire ouvre `http://127.0.0.1:8000` : l'appel devient cross-origin
 *    et EPURE_CORS_ORIGINS ne liste que les origines :5173 (main.py).
 *
 * Le test distingue `undefined` de la chaîne vide — et NON par coquetterie :
 * l'ancien `VITE_API_URL?.replace(…) || 'http://localhost:8000'` ne permettait
 * pas d'exprimer le mode 2. Une chaîne vide est falsy, donc `|| ` retombait sur
 * le défaut : construire avec une valeur vide donnait quand même du localhost:8000.
 *
 * **Sous Windows, `VITE_API_URL=''` est inexprimable** : `$env:VAR = ''`
 * SUPPRIME la variable (mesuré — l'enfant voit « non définie »), et Windows est
 * la plateforme primaire du projet. Le réglage du mode 2 est donc
 * `VITE_API_URL=/`, que le `replace` ci-dessous normalise en chaîne vide. La
 * chaîne vide reste acceptée pour un build POSIX.
 */

const _API_URL = import.meta.env.VITE_API_URL as string | undefined

export const API: string =
  _API_URL === undefined ? 'http://localhost:8000' : _API_URL.replace(/\/+$/, '')

/**
 * En mode 2, `API` est vide : on ne peut pas en dériver le schéma ws. On le
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
