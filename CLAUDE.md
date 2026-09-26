# CLAUDE.md — Épure

Contexte et invariants du dépôt, à lire avant toute modification.
Ce fichier est normatif : ce qui est marqué **IMPÉRATIF** ne se discute pas sans
que l'utilisateur (Ilyann) l'ait validé explicitement dans la conversation.

Noyau volontairement court : invariants + carte du dépôt, pas leur
justification complète (détail, mesures, incidents datés → `docs/claude/`,
table juste après §1 ; design → `docs/`).

---

## 1. Ce qu'est le projet

Assistant d'étude et de travail **local-first**. Chat multi-modèles, RAG sur PDF,
historique, voix — et un **Atelier** qui fait générer par un LLM de nouveaux
modules (backend + frontend) et les monte dans l'application.

**IMPÉRATIF — le cœur est générique.** Il ne présume aucune filière, aucune
matière, aucun métier. Ce qui spécialise une instance, ce sont ses *modules*
(`modules-catalogue/`) et sa configuration — jamais `core/`. Le contraire
s'était installé sans que rien ne le signale ; `backend/test_coeur_generique.py`
tient désormais la frontière. Détail : `docs/claude/contexte-historique.md` §1.

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

### Annexes (`docs/claude/`) — chargées à la demande, pas à chaque session

Le noyau donne la règle et le test qui la verrouille ; l'annexe donne
l'incident et les mesures. Chaque `§` d'origine garde sa section dans ce
noyau (même numérotation), avec le renvoi vers l'annexe qui porte le détail —
un commentaire de code citant `CLAUDE.md §N` (`core/runtime.py`,
`test_taches_locales.py`, `tools/dev-epure.ps1`, `tools/faire_paquet.py`)
reste donc résoluble.

| Tu touches… | Charge |
|---|---|
| Lancement, venv dédié, `verif-ci.ps1`, la suite de tests, l'écart CI/local | `docs/claude/dev-workflow.md` |
| Ingestion d'un document ou d'une image (RAG, module Docs, vision import/chat) | `docs/claude/ingestion-documents.md` |
| Un appel LLM hors du tour de chat (résumé, classification, tâche de fond) | `docs/claude/cloud-local.md` |
| Le module `encre` / la transcription manuscrite (`core/hmer.py`) | `docs/claude/hmer-transcription.md` |
| Le stockage JSON, le store vectoriel, la pile d'embedding | `docs/claude/persistance-embedding.md` |
| Un nouveau chemin de fichier/dossier, ou `_test_env.py` | `docs/claude/chemins.md` |
| `/ws/chat`, SSE, le streaming du LLM, le raisonnement | `docs/claude/sse-websocket.md` |
| L'Atelier : exigences par moteur, effet des interrupteurs de paquet | `docs/claude/atelier-detail.md` |
| Le tool-calling natif (outils du chat, budgets, capacités, LM Studio) | `docs/claude/tool-calling.md` |
| N'importe quoi — avant de conclure qu'un bug est nouveau | `docs/claude/pieges-connus.md` |
| Le pourquoi historique derrière une règle de ce noyau | `docs/claude/contexte-historique.md` |

---

## 2. Lancer et tester

```powershell
# Relance de DEV après un git pull : node résiduels, pull, npm ci (avec
# réparation EPERM), build, libération du port 8000, uvicorn au premier plan.
# Un raccourci de bureau y mène (-PoserRaccourci pour le (re)créer).
.\tools\dev-epure.ps1
.\tools\dev-epure.ps1 -Diagnostic   # tout sauf uvicorn, pour vérifier l'état

# Tout-en-un (Ollama + backend + frontend + tray) — usage normal
python epure_tray.py

# Backend seul (activer le venv dédié d'abord)
.\.venv\Scripts\Activate.ps1
cd backend
python -m uvicorn main:app --reload

# Frontend seul
cd frontend
npm run dev
```

**IMPÉRATIF — le backend tourne dans le venv dédié `.venv/`**, jamais le
Python partagé. Créé/synchronisé par `tools\dev-epure.ps1` et
`epure_tray.py` seuls — ne jamais installer les dépendances backend ailleurs.
Échappatoire, cas particuliers (tray, `core/codeagent.py`) :
`docs/claude/dev-workflow.md`.

**IMPÉRATIF — avant de pousser, lancer `tools\verif-ci.ps1`** (venv activé
pour `-Backend`) : `npm run lint`/la suite backend lancés à la main ne
mesurent PAS le périmètre de la CI. Le script lit `ci.yml` au lieu de le
paraphraser. Détail, deux incidents vécus : `docs/claude/dev-workflow.md`.

Tests : `cd backend && python -m unittest discover -s . -p "test_*.py"` ;
`cd frontend && npm test` (vitest — éprouver la FORME des réponses d'un
backend qui refuse, pas seulement le cas nominal). Liste annotée des ~30
fichiers de test, écarts de version/dépendances CI-local :
`docs/claude/dev-workflow.md`.

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

- **Import à effets de bord assumés** : charge `config.yaml`, instancie les
  moteurs. Ne jamais l'importer depuis un script qui doit rester léger
  (`smoke_runner.py`, `module_worker.py` n'importent **aucun** `core.*`).
- **IMPÉRATIF — `_LazyEngine`** : `rag`, `docanalysis`, `history_engine`,
  `whisper`, `piper` sont des proxies construits au premier accès seulement —
  construire `rag` peut déclencher un téléchargement de 90 Mo qu'on ne veut
  pas au démarrage. **Ne pas « simplifier » en instanciant directement.**
  Détail : `docs/claude/contexte-historique.md` §3.2.
- **`_hf_offline_if_cached()`** doit rester **avant** le premier import de
  `huggingface_hub` (`HF_HUB_OFFLINE` est figée à l'import) — sinon démarrage
  bloqué plusieurs minutes quand le réseau est mauvais.

### 3.3 Anatomie d'un module

Un module = **exactement 3 fichiers** :

```
backend/modules/<id>/manifest.json
backend/modules/<id>/router.py
frontend/src/modules/generated/<id>/Component.tsx   (généré ou installé)
   ou frontend/src/modules/<id>/Component.tsx        (cœur)
```

`manifest.json` : `id`, `version`, `nom`, `icon` (lucide-react), `description`,
`frontend.component`, `backend.prefix`, `core_module`, `origin`, `status`,
`removable`. `modules-catalogue/<id>/` est la **source** des modules
installables (`docs/catalogue-modules.md`) — rien n'y est monté. Installer =
copier vers `backend/modules/<id>/` et `frontend/src/modules/generated/<id>/` ;
le routeur est chargé au **redémarrage** suivant, par
`app.include_router(router, prefix=manifest.backend.prefix)`
(`core/module_registry.register_routers`).

**IMPÉRATIF — aucun montage ni démontage de routes pendant que l'app tourne.**
`register_routers` n'est appelé qu'à l'import de `main.py` ; installer,
approuver, supprimer ou (dés)activer change l'état voulu, et « redémarrage
requis » est l'écart calculé `ecart_redemarrage(app)` (jamais un drapeau
stocké). Le démontage filtrait `app.router.routes`, interne que fastapi 0.137 a
changé (`docs/limite-demontage.md`, `docs/demontage-option-d.md`). Verrouillé
par `test_redemarrage_modules.py`.

**IMPÉRATIF — le prefix de montage est `""` pour les modules générés.** Chaque
route doit être écrite préfixée à la main : `@router.get("/<id>/ping")`. Sans
ça, collision silencieuse avec une route core (`/models`, `/analyze`).

#### Deux états, une seule source de vérité

| État | Source de vérité | Effet | Stockage |
|---|---|---|---|
| **Installé** | `backend/modules/<id>/manifest.json` existe | le module existe pour cette instance | aucun — dérivé du disque |
| **Actif** | `id` ∈ `instance_config.modules_activés` (liste **ordonnée**) | routeur monté **et** visible dans la barre | `memory/instance_config.json` |

Il n'y a **pas** d'état « monté mais invisible ». Actif = les deux à la fois.

**Exception voulue — module de l'Atelier non approuvé** (`origin: workshop`) :
actif mais dont l'empreinte (manifest + router + composant) diffère de celle
enregistrée par `approve()` dans `memory/modules_approuves.json`, ou qui n'en a
jamais eu. Il n'est **ni monté ni rendu** (encart à la place du composant) tant
qu'il n'est pas ré-approuvé ; `GET /modules` le dit (`approbation`). Ce n'est
pas un second état « actif » : c'est un contrôle de contenu, calculé à chaque
lecture (`etat_approbation`). Verrouillé par `test_empreinte_approbation.py`.

**IMPÉRATIF : `backend/memory/modules_state.json` a été supprimé et ne doit
pas être recréé** — deux fichiers pour un même état divergent mécaniquement
(mesuré avant migration : `docs/claude/contexte-historique.md` §3.3). Un
besoin d'état supplémentaire est probablement `installé`, qui se lit sur le
disque. `core/module_registry.py:active_ids()` est la **seule** lecture
d'état ; **toute écriture de `instance_config.json` passe par
`core/jsonstore.transaction()`** (`InstanceConfig._mutate`).

### 3.4 Persistance — aucune base de données côté application

- **IMPÉRATIF : jamais de `json.load`/`json.dump` direct** — toujours
  `core/jsonstore.py` (`backend/memory/`, `backend/history/`).
- **IMPÉRATIF : un seul store vectoriel** (`core/vector_store.py`), construit
  par `core/runtime.py` et **injecté** aux trois moteurs (`fiches`,
  `doc_analysis`, `history`). Ne jamais en instancier un second.
- **IMPÉRATIF : `onnxruntime` s'importe DANS `MoteurEmbedding.__init__`**,
  jamais en tête de `core/embedding.py`.
- **IMPÉRATIF : `onnxruntime` est déclaré en DIRECT dans `requirements.txt`**,
  même déjà transitif. Verrouillé par `test_dependances_declarees.py`.

Migration `sentence-transformers` → `onnxruntime`, retrait de chromadb,
contraintes Smart App Control : `docs/claude/persistance-embedding.md`,
`docs/remplacement-vectoriel.md`.

### 3.5 Chemins

**IMPÉRATIF : aucun chemin absolu en dur.** Tout passe par `core/paths.py`
(`resolve_fiches_dir()`, `resolve_workspace()`, `resolve_data_dir()`,
`resolve_modules_dir()`, `resolve_generated_dir()`, `resolve_web_dir()`,
`resolve_embedding_dir()`, `resolve_hmer_dir()`, `resolve_models_dir()`,
chacune surchargeable par `$EPURE_*`). **Les appeler, jamais figer leur
résultat dans une constante de module ni un défaut d'argument** — verrouillé
par `test_data_dir.py`.

**IMPÉRATIF : `backend/test_zz_donnees_reelles.py` reste le DERNIER module
découvert** (ordre alphabétique) — tout `test_*.py` neuf trie avant `test_zz_`.

Confinement : `Path.resolve()` + `is_relative_to()`, jamais `startswith`
(`codeagent._safe_path`, `test_safe_path.py`). Détail des `resolve_*()` et de
`_test_env.py` : `docs/claude/chemins.md`.

### 3.6 SSE et WebSocket

**IMPÉRATIF — chaque trame de `/ws/chat` porte `conversation_id`** (une seule
connexion sert toutes les conversations d'un onglet). Verrouillé par
`test_chat_conversation_id_trames.py` et `Component.conversation.test.tsx`.

**IMPÉRATIF : tout consommateur de `LLMEngine.stream()` filtre par
`isinstance(item, str)`** avant de concaténer — le flux yielde aussi des
dicts sentinelles (`__stats__`, `__reasoning__`).

**IMPÉRATIF — WebSocket : `await ws_require_token(websocket)` AVANT
`accept()`**, `return` si `False` (`core/auth.py`) — le middleware HTTP ne
s'applique pas aux WebSockets.

Raisonnement (bascule Ollama/FLM), sentinelle `__reasoning__`, limites
connues et acceptées : `docs/claude/sse-websocket.md`.

### 3.7 Le cloud ne part jamais sans qu'on l'ait demandé

**IMPÉRATIF — une tâche qui n'est pas le tour de chat tourne en LOCAL.** Elle
ne part vers un fournisseur distant que sur un choix explicite *pour cette
tâche précise* (`modele_pour_tache(use_cloud, modele_cloud, cle_env)`,
`core/instance.py`), jamais en héritant de `modèle_actif`. `flm` est
**LOCAL** (NPU) malgré son nom. Hors règle : paliers Medium/High de
l'orchestrateur, et l'Atelier (config propre).

Verrouillé par `test_taches_locales.py` (pire cas : `modèle_actif` cloud et
toutes les clés présentes). Historique des six sites fautifs avant le
2026-08-24 : `docs/claude/cloud-local.md`.

**IMPÉRATIF (règle posée le 2026-09-20) — les fournisseurs cloud d'Épure se
paient en crédits prépayés, jamais par abonnement à facturation récurrente.**
Mistral, Groq, Cerebras, NVIDIA, DeepSeek, Gemini : clé API classique sur
solde prépayé. Aucun engagement récurrent à ajouter en configurant un nouveau
fournisseur cloud pour le chat. Sans rapport avec `claude_sub`/
`claude_gateway` (§5) : ces deux-là sont l'authentification CLI de l'Atelier,
pas des fournisseurs cloud du chat.

### 3.8 Transcription manuscrite (`core/hmer.py`) — la seule pile lourde du dépôt

**Un index, pas une sortie** (ExpRate 24,0 %) — texte brut, sans rendu
mathématique, sans post-correction LLM.

**IMPÉRATIF — `core/hmer.py` n'importe QUE la bibliothèque standard au niveau
module** (`optimum`/`transformers`/`Pillow` importés dans les méthodes) —
sinon la collecte de tout test qui importe `main` échoue en CI.

**IMPÉRATIF — `optimum-onnx` et `transformers` restent dans
`HORS_PAQUET_PIP`** (`torch` les accompagne). Verrouillé par
`HmerHorsPaquetTest` (garantit l'absence du LIVRABLE, pas de la déclaration).

Mesures et détail : `docs/claude/hmer-transcription.md`. Ingestion de
documents et vision (RAG vs module Docs, import vs chat) : deux chemins
distincts, journal complet dans `docs/claude/ingestion-documents.md`.

### 3.9 Tool-calling natif — le modèle appelle des outils pendant le tour

Registre `_SKILLS` (`core/llm.py`) : `web_search` et `recherche_approfondie`
(DuckDuckGo + lecture de pages, **réseau**), `history_search` (store
vectoriel, local), plus les skills personnalisés agentiques (renvoient un
texte, rien d'autre). Budgets par tour : 2 / 4 / 2 / 4 par skill.
**`recherche_approfondie` est désactivée par défaut** : seul le bouton du chat
la force, pour un message. Seul le tour de chat direct passe des outils.

- **Fournisseurs : Ollama et LM Studio seuls.** Les six autres de
  `_stream_openai` et Gemini envoient le corps d'avant à l'octet (verrouillé
  par `test_tool_calling_lmstudio.py`).
- **IMPÉRATIF — capacité jamais supposée.** Ollama : `tools` dans
  `/api/tags` ; LM Studio : `trained_for_tool_use is True` dans
  `/api/v1/models` (son mode « par défaut » par prompt est filtré). Inconnu =
  aucun outil.
- **Plafond d'allers-retours** : somme des budgets + 1
  (`_plafond_rounds_outils`, +1 round de grâce sur Ollama) — un outil inconnu
  ne décrémente aucun budget. Pire cas : 9 allers-retours (10 sur Ollama) et
  6 recherches web (+1 du classificateur) avec la recherche approfondie ;
  ≈ 22 s par recherche ; chaque aller-retour renvoie tout le prompt.
- **Coexiste avec le classificateur heuristique** (`@web`), sans le
  remplacer : `rang_web_existant` renumérote les résultats pour que deux
  sources ne partagent jamais un rang `[n]`.
- LM Studio, appel d'outil au JSON cassé : réponse **continuée** sans outil
  (`_continuer_sans_outil`) — **mesuré sur un seul modèle**
  (`ministral-3-3b`), à re-mesurer avant d'en supposer un autre.
- `history_search` (et le reclassement web) peut déclencher le
  **téléchargement automatique du modèle d'embedding (~90 Mo)** s'il est
  absent (`EPURE_EMBEDDING_AUTOINSTALL=0` pour l'interdire).

Détail, chiffres, mesures : `docs/claude/tool-calling.md`.

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
approve       → copie vers modules/<id>/ + generated/<id>/, backup dans _backups/ ; chargé au redémarrage
reject        → rmtree du staging
```

Quatre moteurs de génération (`ollama`, `claude_sub`, `claude_gateway`,
`aider`), diagnostiqués dans Réglages › Atelier. **L'Atelier est
désactivable pour le paquet distribué — désactivable, pas supprimable**
(`EPURE_ATELIER=0` côté backend, `VITE_ATELIER=0` côté build). Détail des
exigences par moteur et de l'effet exact des deux interrupteurs :
`docs/claude/atelier-detail.md`.

**IMPÉRATIF — ne pas supprimer `core/module_workshop.py` ni
`core/module_validate.py` d'un paquet.** `core/catalogue.py` importe sept
symboles du premier, qui importe le second au niveau module : les retirer
casse l'écran Réglages du destinataire, pas seulement l'Atelier.

**IMPÉRATIF — `src/atelier.ts` reste une comparaison directe**
(`import.meta.env.VITE_ATELIER !== '0'`), jamais un `?.trim()` : ça
empêcherait rolldown d'éliminer le code mort de l'Atelier du paquet.
Verrouillé par `test_paquet.py`.

**IMPÉRATIF : ne jamais ajouter de règle à la denylist de
`core/module_validate.py` en croyant renforcer la sécurité.** C'est une
denylist AST contournable par construction (alias de builtin, `Subscript`,
dunder…) — un garde-fou anti-accident, pas une frontière. La vraie frontière
est l'isolation worker (§7). Si tu veux durcir, discute d'abord de
l'isolation.

Conventions imposées au code généré : `backend/modules/_atelier/CONVENTIONS.md`
— c'est le prompt système de fait, à tenir à jour avec tout changement de
contrat.

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
- **IMPÉRATIF : le token d'API ne sort jamais** — ni de `GET /instance/config`,
  ni des logs (le token du WebSocket voyage en query param — uvicorn
  journalise l'URL), ni d'un message d'erreur. Tenu par `core/logs.py`, un
  filtre posé sur `uvicorn.access`, `uvicorn.error` **et** la racine (les deux
  premiers ont `propagate = False`, donc il faut les nommer explicitement).
  Vérifié par `test_logs_secrets.py`.
- Un chemin venant du client est **toujours** `Path(...).name` ou confiné par
  `resolve()` + `is_relative_to()`. Jamais concaténé tel quel.
- Comparaison de token : `hmac.compare_digest` (`core/auth.py`), jamais `==`.

---

## 7. Chantier en cours — isolation des modules générés

`docs/isolation_modules.md` décrit le design ; `docs/etude-isolation-modules.md`
chiffre les options et fixe la politique en vigueur. `core/module_worker.py` et
`test_module_isolation.py` sont suivis et le test passe en CI (découverte
automatique), mais le worker **n'est pas câblé** : `register_routers`
(`core/module_registry.py`) importe encore tous les routers dans le process
principal, `spawn_worker` n'est appelé que par les tests, et aucune route
`/capabilities/*` ni aucun proxy `/<id>/*` n'existe. **`slides`, seul module
`origin: workshop` installé, ne démarrerait pas dans le worker actuel** : il
importe `core.instance`, `core.jsonstore` et `core.paths`, que le garde
d'import refuse.

Conséquence à garder en tête : **aujourd'hui, un module généré tourne avec
`os.environ` (clés API), l'accès à `core.instance` (token) et l'objet `app`.**
Côté navigateur, son composant a les droits de toute l'interface (token,
`/code/execute`).

Ne pas déclarer l'isolation faite tant que : le proxy `/<id>/*` existe dans
`main.py`, les routes `/capabilities/*` existent, `spawn_worker` est appelé en
production, et les modules générés réels (`slides`) démarrent dans le worker.

Déclencheurs de l'isolation complète (worker, AppContainer, iframe) et
politique d'ici là : `docs/feuille-de-route.md` §5.

---

## 8. Pièges connus

Incidents déjà payés une fois, chacun verrouillé par un test — table complète
(BOM UTF-8, `OLLAMA_HOST=0.0.0.0`, démarrage bloqué par HF, PowerShell 5.1
[cp1252, stderr qui termine un script pourtant réussi, guillemets perdus par
`-c`], Smart App Control par fichier/par version, dépendance porteuse
disparue avec le paquet tiers qui l'amenait, `Expand-Archive` qui s'imbrique,
raisonnement Ollama jeté en silence, écrasement de fichier par l'agent de
code, régressions « vert en local, rouge en CI »…) :
**`docs/claude/pieges-connus.md`** — à lire avant de conclure qu'un bug est
nouveau.

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
`backend/history/`, `backend/chroma_db/`, `backend/vector_db/`, `backend/doc_uploads/`,
`backend/modules/_backups/`, `*.log`, `.aider.*`, `.venv/` (le venv dédié, §2 —
déjà dans `.gitignore`).

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
- Installer les dépendances du backend dans le Python **partagé** de la
  machine (§2) — utiliser le venv dédié (`.venv/`), que `dev-epure.ps1` crée
  et synchronise tout seul.
