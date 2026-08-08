# Isolation des modules générés — design v1 (à relire avant implémentation)

## Objectif et frontière de sécurité

Un module `origin="workshop"` + `core_module=false` ne s'exécute plus dans le
process FastAPI principal. Ce qui devient **réellement protégé** : la mémoire
du process hôte — variables d'environnement (clés API), token d'API d'instance,
objets runtime (app, moteurs, config). C'est le prérequis du partage de modules.

**Résiduel assumé en v1 (documenté, pas caché)** : le worker tourne sous le
même utilisateur OS → le système de fichiers n'est pas sandboxé (un module
malveillant pourrait *lire* `backend/memory/instance_config.json` ou `.env`
sur disque). Mitigations : le gate AST reste en place (interdit `os.environ`
non littéral, réseau, subprocess, chaînes suspectes) comme **garde-fou
anti-accident — plus comme frontière** (en-tête de `module_validate.py` mis à
jour en ce sens) ; v2 possible : Job Objects / AppContainer / utilisateur OS
dédié. À confirmer que ce résiduel est acceptable pour v1.

## Périmètre

- **Isolés** : les 10 modules workshop actuels — astral, clicker, dinosaure,
  emojis, minecraft, minuteur, pong, rangement, snake, vroom. Survey : seuls
  pong et rangement utilisent `core.runtime.llm` (generate + stream/SSE) ;
  aucun n'utilise de WebSocket ; rangement écrit `memory/rangement_history.json`.
- **In-process inchangés** : modules core (chat, code, docs…) et `hello`
  (origin builtin). Échappatoire config : `atelier.modules_in_process: [ids]`
  force un module en mode legacy (débogage, compat).

## Architecture

```
client ──token──▶ principal (middleware auth inchangé)
                    ├── modules core : in-process (inchangé)
                    └── ProxyRouter /<id>/* ──HTTP 127.0.0.1:port──▶ worker <id>
                                                                      │
worker ──HTTP 127.0.0.1 /capabilities/* (jeton worker)──▶ principal ◀─┘
```

- Le principal vérifie le token API **avant** de relayer ; le worker ne le
  voit jamais (ni en header — retiré au relais — ni en env).
- Worker = script **autonome** `core/module_worker.py` (pattern smoke_runner :
  aucun import de core) : app FastAPI minimale + router du module, lancé avec
  un env expurgé (`_make_exec_env` sans clés/token/OLLAMA_*, PYTHONPATH réduit
  aux site-packages). Avant l'import du router, `core.runtime` est remplacé
  par un **shim** dans sys.modules ; tout autre `core.*` est refusé (hook
  d'import). → `from core.runtime import llm` continue de marcher tel quel :
  pong et rangement fonctionnent sans modification.

## Choix IPC

| Option | Verdict |
|---|---|
| **TCP loopback, port éphémère/worker** | **Retenu.** Porte HTTP + SSE nativement, uvicorn/httpx OK sous Windows (plateforme primaire). |
| Socket unix | Écarté : support uvicorn/httpx incomplet sous Windows. |
| stdin/stdout | Écarté : ne porte ni SSE ni la concurrence HTTP sans réinventer un framing. |

Défense en profondeur sur le loopback : chaque worker exige l'en-tête
`X-Epure-Worker-Key` (aléatoire par lancement, transmis au worker via SON env —
ce n'est pas un secret du principal) ; symétriquement les routes
`/capabilities/*` du principal exigent le **jeton de worker** (distinct du
token API, un par worker, révoqué à l'arrêt) + client local. Un autre process
local ne peut donc ni piloter un worker ni consommer les capabilities.

## API capabilities (surface v1, volontairement minimale)

| Route (principal) | Contrat | Contrôles |
|---|---|---|
| `POST /capabilities/llm/generate` | `{messages, model?}` → `{text}` | quotas (quota_tracker), modèle résolu côté principal |
| `POST /capabilities/llm/stream` | idem → SSE de tokens | idem |
| `GET/PUT /capabilities/storage` | un document JSON par module | `backend/module_data/<id>.json` via jsonstore, ≤ 512 Ko, **id déduit du jeton de worker** (jamais de la requête) → pas de traversée possible |

Rien d'autre en v1 (pas de rag/memory/flashcards — à exposer explicitement
plus tard si un module en a besoin). Le shim `core.runtime` du worker expose
`llm` (generate/stream → capabilities) et `SSE_HEADERS` ; c'est tout.

## Cycle de vie

- **Démarrage paresseux** au premier hit sur `/<id>/` (Popen + attente
  readiness ≤ 15 s) ; démarrage également déclenché par `approve()` de
  l'atelier (qui ne fait plus d'importlib dans le principal pour ces modules).
- **Timeout** par requête relayée : 120 s (SSE : timeout de connexion
  seulement, pas de limite de durée).
- **Crash** (connexion refusée/reset) → 503 JSON explicite (affiché par le
  ModuleErrorBoundary existant) + redémarrage automatique, max 3/min avec
  backoff.
- **Idle-stop** après 15 min sans requête (récupère la RAM des 10 workers
  potentiels) ; arrêt propre de tous les workers au shutdown (terminate→kill).
- **Limite mémoire** : best-effort v1 — contrôle RSS périodique (psutil si
  présent, sinon désactivé) avec redémarrage au-delà de 500 Mo ; Job Objects
  Windows notés pour v2.

## Tests prévus

1. Module-espion : une route qui dump `os.environ` → aucune clé API, aucun token.
2. Le token API n'est atteignable ni via le proxy (header retiré) ni via
   capabilities (surface fermée, instance_config jamais servi).
3. Storage : tentative d'écrire pour un autre id / traversée → refus, fichier
   confiné à `module_data/`.
4. Crash : route qui `os._exit(1)` → le backend répond toujours, 503 propre,
   worker relancé au hit suivant.
5. Les 10 modules générés répondent à l'identique via le proxy (smoke GET
   généralisé + parcours réels de pong et rangement, SSE compris).
6. Modules core inchangés : `test_modules_mount` + non-régression des tests existants.

## Docs à mettre à jour

- `module_validate.py` : en-tête « garde-fou anti-accident ; la frontière de
  sécurité est l'isolation worker (docs/isolation_modules.md) ».
- `CONVENTIONS.md` : LLM et persistance via le shim (`core.runtime.llm`,
  `core.runtime.storage`) — jamais d'import direct d'autres modules core ;
  documenter la limite de taille du storage.

## Points tranchés à confirmer en relecture

1. TCP loopback plutôt qu'UDS (Windows) — ok ?
2. `hello` (builtin non-core) reste in-process — ok ?
3. Storage v1 = un seul JSON ≤ 512 Ko par module — suffisant ?
4. Idle-stop 15 min + démarrage paresseux (latence à froid ~1-2 s au 1er hit) — ok ?
5. Résiduel filesystem (lecture disque même utilisateur OS) accepté en v1 ?
