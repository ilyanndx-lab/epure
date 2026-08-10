# CLAUDE.md — Épure

Contexte et invariants du dépôt, à lire avant toute modification.
Ce fichier est normatif : ce qui est marqué **IMPÉRATIF** ne se discute pas sans
que l'utilisateur (Ilyann) l'ait validé explicitement dans la conversation.

---

## 1. Ce qu'est le projet

Assistant d'étude et de travail **local-first**. Chat multi-modèles, RAG sur PDF,
historique, voix — et un **Atelier** qui fait générer par un LLM de nouveaux
modules (backend + frontend) et les monte dans l'application.

**IMPÉRATIF — le cœur est générique.** Il ne présume aucune filière, aucune
matière, aucun métier. Ce qui spécialise une instance, ce sont ses *modules*
(`modules-catalogue/`) et sa configuration. Le contraire s'était installé sans
que rien ne le signale : profil élève né en « PTSI2 », `watch_folders` sur
`Maths / Physique-Chimie / SI`, tri de PDF n'acceptant que ces trois matières,
et trois prompts de `core/` parlant d'un « étudiant en prépa ». Quiconque
installait Épure héritait de la filière de son auteur.
`backend/test_coeur_generique.py` tient la frontière — il liste ses tolérances,
qui sont des explications historiques, jamais du comportement.

- **Backend** : FastAPI, `backend/` — port 8000
- **Frontend** : React 19 + Vite + TypeScript + Tailwind, `frontend/` — port 5173
- **LLM** : Ollama local par défaut ; cloud optionnel (Gemini, Groq, Cerebras,
  Mistral, NVIDIA, DeepSeek) via clés dans `backend/.env`
- **Plateforme primaire : Windows.** Tout choix technique qui casse sous Windows
  est un mauvais choix, même s'il est plus élégant sous Linux.

### Contraintes structurantes (à ne jamais perdre de vue)

| Contrainte | Conséquence sur les décisions techniques |
|---|---|
| **Mono-utilisateur**, une seule instance sur le poste d'Ilyann | Pas de multi-tenant, pas de rôles, pas de RBAC. Un seul token d'API. |
| **Local-first**, aucun serveur distant | Pas de dépendance à un service hébergé. Le cloud LLM est optionnel et dégradable. |
| **L'Atelier est le cœur du projet**, pas un outil de dev | Tout ce qui touche à la génération/validation/montage de modules est du code de production, à traiter avec le même soin que `core/`. |
| **Auteur seul, en prépa** | Interruptions de plusieurs semaines. Le code doit être relisible à froid : docstrings en français expliquant *pourquoi*, pas *quoi*. C'est déjà la convention du dépôt — la respecter. |

---

## 2. Lancer et tester

```powershell
# Tout-en-un (Ollama + backend + frontend + tray) — usage normal
python epure_tray.py

# Backend seul
cd backend
python -m uvicorn main:app --reload

# Frontend seul
cd frontend
npm run dev
```

### Tests

Les tests sont des scripts `unittest` **autonomes à la racine de `backend/`**
(pas de dossier `tests/`, pas de pytest). Chacun fait son propre
`sys.path.insert(0, dirname(__file__))`.

```powershell
cd backend
python -m unittest discover -s . -p "test_*.py"   # la commande de la CI
python test_module_validate.py    # gate AST des routers générés
python test_safe_path.py          # confinement de chemin du codeagent
python test_jsonstore.py          # lecture/écriture JSON (BOM)
python test_module_states.py      # deux états des modules + migration (§3.3)
python test_web_search.py         # recherche web, HTTP mocké
python test_web_statique.py       # interface servie par FastAPI (paquet distribué)
python test_logs_secrets.py       # le token ne sort pas dans les logs (§6)
python test_module_isolation.py   # worker isolé — CHANTIER, cf. §7
python integration_modules_mount.py  # LOURD : core.runtime (torch, chromadb)
```

**Un nouveau `backend/test_*.py` est pris en compte sans toucher au workflow** :
la CI tourne en `unittest discover` depuis le commit `7e3bf8c`. Ce n'est plus une
liste de `run:` nommés — cette liste avait laissé 4 fichiers sur 6 ne jamais
tourner. Nommer un fichier `integration_*.py` au lieu de `test_*.py` est ce qui
l'exclut de la découverte (cas de `integration_modules_mount.py`, qui charge
torch et chromadb et tourne dans le job `integration`, manuel).

### Écart de version Python — piège actif

Les `.pyc` locaux sont en `cpython-314` (Python 3.14) ; la CI tourne en **3.12**
(`ci.yml`). Du code qui marche en local peut casser en CI. Si tu utilises une
syntaxe récente, vérifie sa disponibilité en 3.12.

---

## 3. Architecture backend

### 3.1 Chaîne de démarrage

```
main.py
 ├─ logging (EPURE_LOG_LEVEL)
 ├─ app = FastAPI()
 ├─ middleware _require_api_token        ← token exigé partout sauf /health et /pair
 ├─ CORSMiddleware (EPURE_CORS_ORIGINS, jamais "*")
 ├─ @app.exception_handler(Exception)    ← JSON 500 uniforme
 ├─ from core.runtime import ...         ← EFFETS DE BORD : instancie tous les moteurs
 ├─ routes racine encore dans main.py    ← /models, /instance/*, /modules, /workshop/*, /pair, /ws/workshop
 └─ _register_routers(app)               ← monte modules/<id>/router.py
```

### 3.2 `core/runtime.py` — le point d'entrée de l'état partagé

**IMPÉRATIF : c'est la seule source des moteurs. On n'instancie jamais un moteur
ailleurs, et on n'importe jamais `core.llm`/`core.rag`/… directement depuis un
module.**

```python
from core.runtime import llm, rag, memory, SSE_HEADERS
```

Points de conception à respecter :

- **Import à effets de bord assumés** : charge `config.yaml`, instancie les
  moteurs, reset le contexte de session, lance un thread de préchauffage.
  Importer `core.runtime` n'est jamais gratuit — ne pas le faire depuis un script
  qui doit rester léger (c'est pourquoi `smoke_runner.py` et `module_worker.py`
  n'importent **aucun** `core.*`).
- **`_LazyEngine`** : `rag`, `docanalysis`, `history_engine`, `whisper`, `piper`
  sont des proxies. Le moteur réel n'est construit qu'au premier accès à un
  attribut. Raison : `RAGEngine` importe torch + sentence-transformers (~30 s à
  chaud, 2 min à froid) et bloquait uvicorn au point que `/health` ne répondait
  pas. **Ne pas « simplifier » en instanciant directement.**
- **`_hf_offline_if_cached()`** doit rester **avant** le premier import de
  `huggingface_hub` : `HF_HUB_OFFLINE` est figée à l'import. Déplacer cette
  fonction ou les imports en dessous réintroduit un démarrage bloqué plusieurs
  minutes quand le réseau est mauvais.

### 3.3 Anatomie d'un module

Un module = **exactement 3 fichiers** :

```
backend/modules/<id>/manifest.json
backend/modules/<id>/router.py
frontend/src/modules/generated/<id>/Component.tsx   (généré ou installé)
   ou frontend/src/modules/<id>/Component.tsx        (cœur)
```

Troisième emplacement depuis l'étape C : **`modules-catalogue/<id>/`**, qui
réunit les trois fichiers côte à côte (`manifest.json`, `router.py`,
`Component.tsx`). C'est la **source** des modules installables, pas une
instance : rien n'y est monté. Installer = copier vers `backend/modules/<id>/`
et `frontend/src/modules/generated/<id>/`. Les six qui y sont
(`code`, `docs`, `flashcards`, `kholle`, `reviseur`, `rangement`) portent
`core_module: false`, `origin: "catalogue"`, `removable: true` — `origin`
distinct de `"workshop"` pour que l'Atelier ne les propose pas à la ré-édition
comme du code jetable.

`manifest.json` : `id`, `version`, `nom`, `icon` (nom lucide-react), `description`,
`frontend.component`, `backend.prefix`, `core_module`, `origin`, `status`,
`removable`.

Montage (`core/module_registry.py:74`) : pour chaque manifeste `status="active"`
possédant un `router.py`, `importlib.import_module(f"modules.{mid}.router")` puis
`app.include_router(router, prefix=manifest.backend.prefix)`.

**IMPÉRATIF — le prefix de montage est `""` pour les modules générés.** Donc
chaque route doit être écrite préfixée à la main : `@router.get("/<id>/ping")`.
Sans ça, collision silencieuse avec une route core (`/models`, `/analyze`).

#### Deux états, une seule source de vérité

| État | Source de vérité | Effet | Stockage |
|---|---|---|---|
| **Installé** | `backend/modules/<id>/manifest.json` existe | le module existe pour cette instance | aucun — dérivé du disque |
| **Actif** | `id` ∈ `instance_config.modules_activés` (liste **ordonnée**) | routeur monté **et** visible dans la barre, à la position donnée par la liste | `memory/instance_config.json` |

Il n'y a **pas** d'état « monté mais invisible ». Actif = les deux à la fois.

**IMPÉRATIF : `backend/memory/modules_state.json` a été supprimé et ne doit pas
être recréé.** Il portait un second `status` par module, en doublon de
`modules_activés`. Deux fichiers pour une notion divergent mécaniquement — c'est
ce qui a été mesuré avant migration : 9 des 11 entrées de `modules_state.json`
pointaient des modules effacés, 4 des 12 entrées de `modules_activés` aussi, et
`reviseur` était installé et monté tout en étant absent de la barre. Si tu crois
avoir besoin d'un état supplémentaire, c'est probablement `installé` que tu
cherches, et il se lit sur le disque.

Règles à respecter :

- `core/module_registry.py:active_ids()` est la **seule** lecture d'état. Liste
  vide → tous les modules installés (défaut d'installation neuve, ordre
  `discover_manifests`, donc alphabétique et déterministe).
- `set_status(id, "active"|"disabled")` ajoute/retire dans la liste. Signature et
  endpoint `PUT /modules/{id}/status` conservés.
- Le champ `status` de `GET /modules` reste `"active"|"disabled"` : il est
  **dérivé** de l'appartenance à la liste. Le frontend en dépend
  (`src/modules.ts`, `ModuleManifest.status`) — ne pas le renommer.
- `settings` ne peut pas être désactivé : refusé par `set_status`, et réinjecté à
  l'écriture (`core/instance.py:_garder_settings`) comme à la lecture. La liste
  pilote le montage : la lui faire perdre débranche l'écran qui sert à la
  réparer.
- **Toute écriture de `instance_config.json` passe par
  `core/jsonstore.transaction()`** (`InstanceConfig._mutate`). Cette liste
  conditionne le démarrage ; un read-modify-write non verrouillé y perd des
  écritures.

### 3.4 Persistance

Aucune base de données côté application. Deux stockages :

- **Fichiers JSON** sous `backend/memory/` et `backend/history/`, via
  **`core/jsonstore.py` — IMPÉRATIF : jamais de `json.load`/`json.dump` direct.**
  Lecture en `utf-8-sig` (un BOM posé par PowerShell 5.1 rendait la mémoire de
  session invisible, puis le fichier était écrasé), écriture en `utf-8` sans BOM.
- **ChromaDB** sous `backend/chroma_db/` pour le RAG et l'historique sémantique.

### 3.5 Chemins

**IMPÉRATIF : aucun chemin absolu en dur.** Tout passe par `core/paths.py` :

- `FICHES_DIR` / `resolve_fiches_dir()` — `$EPURE_FICHES_DIR`, sinon `<repo>/data/fiches`
- `resolve_workspace()` — `$EPURE_WORKSPACE`, sinon `<repo>/workspace`, toujours `.resolve()`
- `resolve_data_dir()` — `$EPURE_DATA_DIR`, sinon `<backend>/memory`
- `resolve_modules_dir()` — `$EPURE_MODULES_DIR`, sinon `<backend>/modules`
- `resolve_generated_dir()` — `$EPURE_GENERATED_DIR`, sinon
  `<repo>/frontend/src/modules/generated`. Le parent (`frontend/src/modules`)
  s'en déduit par `.parent` : une seule variable pour les deux, sinon un
  `generated/` détourné sous un parent resté en place ferait chercher le
  composant d'un module core dans un arbre et son composant généré dans un autre.
- `resolve_web_dir()` — `$EPURE_WEB_DIR`, sinon `<repo>/frontend/dist`. Frontend
  **construit** que FastAPI sert lui-même dans le paquet distribué
  (`docs/distribution-empaquetee.md` étape A). Le service est **éteint** si le
  dossier n'a pas d'`index.html` : c'est le mode développement, où Vite sert
  l'interface. Surchargeable non pour protéger des données mais pour rendre la
  suite **déterministe** — sans ça son comportement dépendrait de la présence
  d'un `npm run build` sur le poste, et un test de l'interface servie passerait
  en local pour échouer en CI.
- `resolve_models_dir()` — `$EPURE_MODELS_DIR`, sinon `<backend>/piper_models`.
  **C'est un cache de modèles, pas des données utilisateur**, et la distinction
  a des conséquences. Le `.onnx` de Piper (76 Mo) y est téléchargé au premier
  usage de la voix puis vérifié par sha256 : le contenu est reconstructible à
  l'identique, rien d'irremplaçable n'y vit. Il est donc délibérément **absent**
  de `_test_env.REAL_DIRS`, la liste surveillée par `test_zz_donnees_reelles` —
  un téléchargement légitime pendant la suite y écrirait 76 Mo et ferait tomber
  un garde-fou qui parle d'autre chose. Il est en revanche bien **détourné** par
  `_test_env` : ne pas confondre « non surveillé » et « laissé au vrai chemin ».
  Avant, `PiperEngine` recevait `models_dir="piper_models"` — un chemin
  **relatif au cwd**, qui ne fonctionnait que parce qu'`epure_tray.py` lance
  uvicorn depuis `backend/`.

**Tous** suivent la même règle. **IMPÉRATIF : les appeler, jamais figer leur
résultat dans une constante de module** — ni dans un défaut d'argument,
`def f(p=CONST)` étant évalué à l'import (c'est sous cette forme que le piège
s'était glissé dans `InstanceConfig` et `QuotaTracker`). Neuf modules
calculaient `Path(__file__).parent.parent / "memory" / …` au chargement : la
suite écrivait donc dans les données réelles, au point d'exécuter pour de bon la
migration de `modules_activés` sur la config de l'utilisateur. Verrouillé par
`test_data_dir.py`, qui pose les variables **après** les imports et vérifie que
l'écriture suit.

**Corollaire à ne pas rater : ne jamais remonter depuis un dossier de données
pour obtenir une racine de code.** `MODULES_DIR.parent.parent` donnait la racine
du dépôt tant que `MODULES_DIR` n'était pas déplaçable ; il l'est désormais.
Utiliser `core.paths.REPO_ROOT` et `core.paths.BACKEND_DIR`, qui sont des anchors
statiques dérivés de `__file__` et n'ont pas de surcharge d'environnement.

Tout test qui importe `core.*` ou `main` doit faire `import _test_env` **avant**
ces imports. `backend/_test_env.py` pose les **cinq** variables sur des
temporaires uniques pour la session — `backend/modules/` et
`frontend/src/modules/` y sont **copiés** (sans `_backups`) pour que les tests
voient un arbre réaliste. C'est ce qui rend `DELETE /settings/modules/{id}`
testable : son `rmtree` frappe la copie. `EPURE_MODELS_DIR` et `EPURE_WEB_DIR`
sont posés sur des temporaires **vides**, pour deux raisons distinctes : copier
76 Mo de modèle vocal n'aurait aucun sens et aucun test ne le lit (détourné
seulement pour qu'un test construisant `PiperEngine` par accident ne tire pas
76 Mo dans le cache réel) ; `frontend/dist/` est vidé pour le **déterminisme** —
`main._register_web` ne monte l'interface que s'il y trouve un `index.html`, donc
sur le vrai chemin la suite se comporterait différemment selon que le front a été
construit sur le poste. `test_web_statique.py` fabrique son propre `dist/`.

**IMPÉRATIF — `backend/test_zz_donnees_reelles.py` doit rester le DERNIER module
découvert.** Son `zz` n'est pas décoratif : `unittest discover` exécute les
modules dans l'ordre alphabétique, et un garde-fou qui vérifie que personne n'a
sali `backend/memory/` ne vaut que s'il passe après tous les autres. Le contrôle
vivait dans `test_data_dir.py` (3e sur 12) : un fichier écrit par
`test_workshop_paths` (12e) laissait la suite verte — 179 tests OK avec un
intrus sur le disque, mesuré. Donc : **tout nouveau fichier de test doit trier
avant `test_zz_`** (c'est le cas de tout nom ne commençant pas par `test_z`).
L'invariant est lui-même testé (`test_ce_module_est_bien_le_dernier_decouvert`).

Ce que le garde-fou ne couvre pas, et qu'il ne faut pas lui prêter : un
`tearDownModule`/`tearDownClass` qui s'exécuterait après lui, les `atexit`, et
les threads démons (`QuotaTracker` en lance un). Il prouve qu'aucun *test* n'a
écrit, pas qu'aucune *ligne de code* n'écrira.

Le confinement se fait par **`Path.resolve()` puis `is_relative_to()`**, jamais
par `startswith` de chaînes (contournable par un dossier frère `modules-autre/`).
Référence correcte : `codeagent._safe_path`, couverte par `test_safe_path.py`.

### 3.6 SSE et WebSocket

- SSE : `StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)`
  où `SSE_HEADERS` vient de `core.runtime` (`Cache-Control: no-cache`,
  `X-Accel-Buffering: no` — indispensable derrière nginx).
- WebSocket : le middleware HTTP ne s'applique pas. **IMPÉRATIF : appeler
  `await ws_require_token(websocket)` AVANT `accept()`** et `return` si False
  (`core/auth.py`). Le token arrive en query param `?token=` parce que les
  navigateurs n'autorisent pas d'en-tête sur `new WebSocket()`.

---

## 4. Architecture frontend

### 4.1 Accès à l'API — un seul chemin

**IMPÉRATIF : tout appel réseau passe par `src/api.ts`.** Jamais de `fetch()` nu,
jamais d'URL en dur.

```ts
import { API, apiFetch, wsUrl } from '../../../api'
const res = await apiFetch(`${API}/<id>/analyze`, { method: 'POST', body: ... })
const ws  = new WebSocket(wsUrl('/ws/<id>'))
```

`apiFetch` joint le token d'instance (récupéré une fois via `GET /pair`, conservé
sous `localStorage['epure.apiToken']`). `wsUrl` l'ajoute en query param.

### 4.2 Résolution des modules

`src/modules/registry.ts` résout `id → composant React` :

- modules core : `lazy(() => import('./chat/Component'))`, liste `CORE_DEFS` en dur ;
- modules générés : `import.meta.glob(['./generated/**/*.tsx', '!./generated/_*/**'])`,
  l'id est le segment `path.split('/')[2]`.

Les dossiers `_*` sont exclus volontairement : le type-check de l'Atelier crée
puis supprime `_workshop_check_<id>/`, et sa simple apparition dans le glob fait
recharger toute la page en pleine revue.

Les **métadonnées d'affichage** (label, icône, ordre, status) viennent du backend
(`GET /modules` → `src/modules.ts`), pas de `registry.ts` — qui ne sert que de
repli.

`ModuleErrorBoundary` isole le **rendu** d'un module planté. Il ne capte ni les
erreurs asynchrones, ni celles des handlers d'événements.

---

## 5. L'Atelier (`core/module_workshop.py`, `frontend/src/components/Workshop.tsx`)

Cycle de vie d'un module généré :

```
prepare(id)   → backend/modules/_staging/<id>/ + .workshop.json
generate      → un moteur écrit router.py / manifest.json / Component.tsx
validate      → core/module_validate.py (gate AST + tsc best-effort)
approve       → copie vers modules/<id>/ + generated/<id>/, importlib, backup dans _backups/
reject        → rmtree du staging
```

Trois moteurs de génération, diagnostiqués dans Réglages › Atelier :

| Moteur | Exigence |
|---|---|
| `ollama` | toujours disponible, modèle actif de l'instance |
| `claude_sub` | CLI `claude` + `claude setup-token`. **Ne pas définir `ANTHROPIC_API_KEY`** — elle primerait sur l'abonnement. |
| `claude_gateway` | CLI `claude` + passerelle Anthropic-compatible locale (LiteLLM exposant `/v1/messages`). `ANTHROPIC_BASE_URL` pointé dessus. |

Un moteur `aider` existe également (mode architect, conversation Plan/Construire).

**IMPÉRATIF : ne jamais ajouter de règle à la denylist de `core/module_validate.py`
en croyant renforcer la sécurité.** C'est une denylist AST sur des noms exacts :
elle est contournable par construction (alias de builtin, `Subscript`, dunder,
`import sys`/`builtins`/`asyncio`, `dict(os.environ)`). Elle est un **garde-fou
anti-accident**, pas une frontière. La vraie frontière est l'isolation worker
(§7). Si tu veux durcir, discute d'abord de l'isolation.

Les conventions imposées au code généré sont dans
`backend/modules/_atelier/CONVENTIONS.md` — c'est le prompt système de fait.
Toute évolution du contrat d'un module doit y être répercutée.

---

## 6. Sécurité — modèle de menace réel

Mono-utilisateur ne veut pas dire « pas d'adversaire ». Les menaces réelles :

1. **Une page web visitée par Ilyann.** Le DNS rebinding rend un domaine
   attaquant *same-origin* avec `127.0.0.1` : CORS ne protège pas, et tout
   endpoint atteignable sans token (ou après vol du token via `/pair`) devient
   exploitable. → `TrustedHostMiddleware`, et aucun endpoint qui exécute une
   commande.
2. **Le LLM lui-même.** Un module généré peut lire `.env`, poster une clé API
   vers une URL inventée, ou détruire un dossier — sans intention malveillante.
   → isolation worker, pas denylist.
3. **Le réseau local** si le backend écoute sur `0.0.0.0` (wifi de la prépa).

Règles :

- **IMPÉRATIF : aucun `shell=True`.** `subprocess.Popen(["binaire", arg1, ...])`,
  toujours en liste. Une entrée utilisateur ne doit jamais atteindre un shell.
- **IMPÉRATIF : le token d'API ne sort jamais** — ni de `GET /instance/config`
  (le bloc `auth` est retiré), ni des logs, ni d'un message d'erreur. La partie
  « logs » n'était pas tenue et ne pouvait pas se voir en relisant Épure : la
  ligne fuyante est écrite par **uvicorn**, qui journalise le chemin avec sa
  query (`"WebSocket /ws/chat?token=…" [accepted]`), et le token du WebSocket
  voyage en query param faute d'en-tête possible sur `new WebSocket()`. Tenu
  désormais par `core/logs.py`, un filtre de logging posé sur `uvicorn.access`,
  `uvicorn.error` et la racine — ces deux loggers ont leurs propres handlers et
  `propagate = False`, donc **il faut les nommer**, un filtre sur la racine ne
  les voit pas. Vérifié par `test_logs_secrets.py`, qui affirme aussi que
  `main` l'installe (sinon le module resterait parfait et jamais appelé).
- Un chemin venant du client est **toujours** `Path(...).name` ou confiné par
  `resolve()` + `is_relative_to()`. Jamais concaténé tel quel.
- Comparaison de token : `hmac.compare_digest` (`core/auth.py`), jamais `==`.

---

## 7. Chantier en cours — isolation des modules générés

`docs/isolation_modules.md` décrit le design. `core/module_worker.py` et
`test_module_isolation.py` existent (untracked à ce jour) mais **ne sont pas
câblés** : `module_registry.py:95` importe encore tous les routers dans le process
principal. Aucune route `/capabilities/*` n'existe.

Conséquence à garder en tête : **aujourd'hui, un module généré tourne avec
`os.environ` (clés API), l'accès à `core.instance` (token) et l'objet `app`.**

Ne pas déclarer l'isolation faite tant que : le proxy `/<id>/*` existe dans
`main.py`, les routes `/capabilities/*` existent, `spawn_worker` est appelé en
production, et `test_module_isolation.py` tourne en CI.

---

## 8. Pièges connus (déjà payés une fois — ne pas les rejouer)

| Piège | Règle |
|---|---|
| BOM UTF-8 dans les JSON de runtime | Toujours `core/jsonstore.py`. Lecture `utf-8-sig`. |
| `OLLAMA_HOST=0.0.0.0` | Casse le client Python Ollama. Toujours une URL complète `http://hôte:11434`. `core/llm.py` normalise ; `core/admin.py` ne le fait pas encore. |
| Démarrage bloqué plusieurs minutes | HF valide son cache au boot. Voir `_hf_offline_if_cached()`, §3.2. |
| `uvicorn --reload` sous Windows | Instable. Restreint à `--reload-dir core` dans `epure_tray.py`, désactivable par `EPURE_RELOAD=0`. |
| Rechargement intempestif de la page en pleine revue Atelier | Les dossiers `_*` sont exclus du glob de `registry.ts`. Ne pas « nettoyer » ce filtre. |
| Mojibake dans les logs aider | Décodage explicite en UTF-8 du stdout. |
| Sortie LLM non parsable | `json.loads(..., strict=False)` pour tolérer les retours ligne des modèles locaux ; strip des balises placeholder recopiées par le parseur Ollama. |

---

## 9. Conventions de contribution

**Commits** : conventional commits, **description en français**, un sujet par
commit. Types utilisés dans l'historique : `feat`, `fix`, `chore`, `docs`, `ci`,
`perf`, `refactor`, `dev`. Scopes courants : `atelier`, `security`, `modules`,
`backend`, `core`, `memory`, `settings`, `quotas`, `catalog`, `tray`.

```
fix(security): confinement de chemin robuste + workspace portable
feat(atelier): boucle de test-réparation (smoke test isolé + correction auto)
```

Quand un commit corrige un bug non évident, la ligne de sujet dit **le symptôme**,
pas seulement la cause : `fix(memory): lecture tolérante au BOM — un BOM rendait
la mémoire de session invisible`.

**Branches** : `main` + branches thématiques. Pas de push direct sur `main` pour
un lot de plusieurs commits — branche puis PR.

**Docstrings** : en français, au niveau module, expliquant la contrainte ou
l'incident qui justifie le design. C'est la convention la plus précieuse du
dépôt : elle rend le code relisible après trois semaines d'absence. La respecter.

**Ne jamais committer** : `backend/.env`, `backend/memory/*.json`,
`backend/history/`, `backend/chroma_db/`, `backend/doc_uploads/`,
`backend/modules/_backups/`, `*.log`, `.aider.*`.

---

## 10. Ce qu'il ne faut pas faire

- Ajouter des règles à `module_validate.py` en pensant sécuriser (§5).
- Instancier un moteur hors de `core/runtime.py`.
- Écrire un JSON de runtime sans passer par `core/jsonstore.py`.
- Ajouter un `shell=True`, même « juste pour Windows ».
- Écrire un chemin absolu en dur (`C:\Users\Ilyan\...`). Le dernier vivait dans
  `start.ps1`, retiré du dépôt pour cette raison ; il n'en reste aucun.
- Rendre `_LazyEngine` « plus simple » en instanciant directement.
- Recréer `backend/memory/modules_state.json` (§3.3) — ou tout second stockage
  de l'état « actif » à côté de `modules_activés`.
- Élargir le périmètre fonctionnel tant que la CI ne peut pas dire non
  (1 `response_model` sur 103 endpoints à ce jour).
