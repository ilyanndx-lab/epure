# Distribution empaquetée — le proche n'installe plus d'outils de développement

**Objectif.** Un proche télécharge une archive, la dézippe, lance un installeur minimal,
double-clique un raccourci. Aucun git, aucun Node, aucun Python visible. Il active ou
désactive dans ses Réglages uniquement les modules qu'Ilyann lui a envoyés — jamais un
catalogue commun, jamais les modules faits pour quelqu'un d'autre.

**Ce qui change de fond.** Jusqu'ici, l'installeur (`docs/installeur.md`) posait
l'environnement de développement complet chez le proche, parce qu'il était censé créer
ses propres modules via l'Atelier. Ce n'est plus le cas : **Ilyann crée les modules,
le proche installe et utilise.** La conséquence directe : l'exécutable packagé, écarté
dans `docs/installeur.md` §5 au motif que « l'Atelier a besoin d'un environnement Python
éditable », perd sa principale raison d'être écarté. Il en reste une seule : la taille.
Une taille de téléchargement n'est pas un blocage, c'est un inconvénient à annoncer.

**Ce que ça ne résout pas.** Le mur rencontré le 2026-08-10 (`chroma-hnswlib`, `grpcio` :
aucune wheel `win_arm64`) ne disparaît pas, il se déplace. Construire le paquet pour une
architecture donnée demande de le construire *sur* cette architecture. Ilyann redevient
nécessaire une fois, comme poste de build — plus jamais comme dépanneur en direct chez
le proche.

> **Mesuré depuis (cf. §0) : la moitié de ce mur n'existe plus.** `chromadb 1.5.9` ne
> dépend plus de `chroma-hnswlib` — l'index ANN est passé à des bindings Rust
> précompilés (`chromadb_rust_bindings`). Il ne reste que `grpcio`, qui a bien une wheel
> `cp312-win_amd64`. Le cas `win_arm64` reste entier et non testé : ce qui précède a été
> mesuré sur x64.
>
> **Mesuré depuis, seconde fois (2026-08-10, cf. §0.5) : le quart qui restait de ce mur
> n'existe plus non plus, mais pas parce qu'une wheel `win_arm64` de `grpcio` a été
> trouvée — parce que `grpcio` n'est plus installé du tout, sur aucune architecture.**
> `google-generativeai` est retiré à l'installation (`HORS_PAQUET_PIP`), et `grpcio` /
> `opentelemetry-exporter-otlp-proto-grpc` / `googleapis-common-protos` — que chromadb
> déclare en dépendances directes et que retirer de `requirements.txt` ne suffit donc pas
> à écarter — sont purgés du `site-packages` après coup. La question « existe-t-il une
> wheel `grpcio` pour `win_arm64` » ne se pose donc plus. Ce qui reste non résolu pour
> `win_arm64` : les AUTRES extensions natives du paquet (`chromadb_rust_bindings`,
> `ctranslate2`, `onnxruntime`, `av`, `numpy`, `PIL`, `piper-tts`) — cf. §0.5 et le nouveau
> §7 sur le profil ARM64.

**Précisions fixées après relecture (à ne pas redemander) :**
- **Python visé : 3.12**, aligné sur `docs/installeur.md` étape B et sur la CI.
- **torch (et les modèles du pipeline RAG) ne sont pas embarqués dans le paquet.**
  Téléchargés au premier lancement, même logique que le modèle vocal Piper
  (`docs/purge-onnx.md`) : le paquet initial reste léger ; le prix est qu'une connexion
  réseau est nécessaire au premier usage du RAG documentaire, ce qui n'est pas une
  contrainte nouvelle — `install.ps1` tire déjà le modèle Ollama en ligne.

---

## État au 2026-08-10

| Étape | État |
|---|---|
| §0 — les quatre vérifications | **faites**, résultats ci-dessous. Aucune n'invalide l'approche |
| A — frontend construit servi par FastAPI | **faite** — `src/api.ts` corrigé, `main._register_web`, `core.paths.resolve_web_dir`, `test_web_statique.py` (10 tests). Vérifiée dans un vrai navigateur, profil neuf |
| B — script de constitution du paquet | **faite** — `tools/faire_paquet.py`, hors de ce qui est livré ; `backend/test_paquet.py` (35 tests) tient les règles d'exclusion, dont la purge de `grpcio`/`opentelemetry-exporter-otlp-proto-grpc`/`googleapis-common-protos` (§0.5) |
| C — installeur minimal | **faite le 2026-08-23** — `tools/installer-epure.ps1` + `tools/Installer-Epure.cmd`, copiés à côté de l'archive à chaque assemblage ; lanceur (`Epure.cmd` + `demarrer.py`) et raccourci Bureau générés ; réinstallation = mise à jour, vérifiée à l'exécution. `backend/test_installeur.py` (28 tests). **Sans** installation d'Ollama : voir « ce qui n'est pas fait » |
| D — Réglages du proche : catalogue restreint | à faire — déjà partiellement acquis, cf. fin du §0 |
| E — documentation | à faire |

---

## §0 — Vérifications préalables — **faites le 2026-08-10**

Les quatre points conditionnaient la faisabilité du reste. Aucun ne l'invalide. Mesuré sur
ce poste : Windows 11, **x64** (le mur `win_arm64` concernait la machine du destinataire,
pas le poste de build).

### 1. Frontend construit servi par FastAPI — **faisable, un changement de code requis**

`npm run build` (`tsc -b` + vite) passe en 2,7 s → `dist/` = 3,7 Mo, 151 fichiers, assets
en chemins absolus `/assets/…` (base vite par défaut) : correct à la racine, cassé sous un
sous-chemin.

**Aucun routing SPA à gérer** : pas de `react-router` dans `package.json`, aucun
`location`/`history.pushState`/`hash` dans `App.tsx` ni `main.tsx`. Une seule URL, `/`.
Donc **pas de catch-all** — et il ne faut surtout pas en poser un, cf. étape A.

**Les appels API étaient en URL absolue non désactivable.** `src/api.ts` faisait
`VITE_API_URL?.replace(…) || 'http://localhost:8000'` : la chaîne vide étant falsy, on ne
pouvait pas demander des appels relatifs. Laisser le défaut ne marche que si le
destinataire ouvre exactement `http://localhost:8000` ; sur `http://127.0.0.1:8000`
l'appel devient cross-origin et `EPURE_CORS_ORIGINS` ne liste que les origines `:5173`
(`main.py:98`). Corrigé — cf. étape A. (`TrustedHostMiddleware` accepte déjà
`localhost,127.0.0.1,::1`, `main.py:133` : le problème était CORS, pas l'en-tête `Host`.)

### 2. Python embeddable + extensions natives — **ÇA PASSE**

C'était le point qui pouvait tout invalider.

`python-3.12.10-embed-amd64.zip` (10,6 Mo — dernière 3.12 à avoir une release binaire
Windows), `#import site` décommenté dans `python312._pth`, `pip` amené par `get-pip.py`.
**22 imports sur 22 réussissent**, soit tout `backend/requirements.txt` hors
`sentence-transformers` : `fastapi starlette uvicorn multipart httpx ollama yaml pypdf
watchdog dotenv google.generativeai openai docx pystray chromadb faster_whisper
ctranslate2 onnxruntime pandas PIL piper grpc`. Aucune compilation : tout arrive en wheel
(`cp312`, `cp39-abi3`, `cp310-abi3`).

Test fonctionnel et pas seulement d'import : `chromadb` en `EphemeralClient` **et** en
`PersistentClient` — `add`, `query`, `count`, `chroma.sqlite3` écrit sur disque.

Le décommentage d'`import site` est bien indispensable, comme anticipé : avant, `sys.path`
ne contenait que `python312.zip` et le dossier, et `import pip` levait `ModuleNotFoundError`.

**`chroma-hnswlib` n'existe plus.** `pip show chromadb` (1.5.9) ne le liste pas : l'index
ANN est passé aux bindings Rust précompilés `chromadb_rust_bindings` (60,5 Mo). C'est la
moitié du mur du 2026-08-10 qui tombe d'elle-même. Reste `grpcio`, qui a une wheel
`cp312-win_amd64`.

Un accident à connaître, traité à l'étape C : **un blocage Smart App Control transitoire**
au premier chargement d'une DLL non signée fraîchement écrite.

### 3. Poids réel sans torch — **186,6 Mo à télécharger**

Paquet assemblé pour de vrai : runtime embeddable + `site-packages` sans
`pip`/`setuptools`/`__pycache__` + `frontend/dist` + code de `backend/` sans les données.

| | non zippé | zippé |
|---|---|---|
| avec `kubernetes` | 610,4 Mo | 190,9 Mo |
| **sans `kubernetes`** (cf. étape B) | **572,6 Mo** | **186,6 Mo** |

Le gain net est donc de 37,8 Mo sur disque mais de **seulement 4,3 Mo au téléchargement** :
`kubernetes` est presque tout du source Python, qui se compresse. Il vaut d'être retiré
pour l'empreinte disque et pour ne pas embarquer un client d'orchestration dans une
application locale, pas pour la taille de l'archive.

Top du paquet (Mo) : `googleapiclient` 97,9 · `av.libs` 62,6 · `chromadb_rust_bindings`
60,5 · `ctranslate2` 59,4 · `numpy`+`numpy.libs` 39,6 · `onnxruntime` 38,6 · `pandas` 33,1
· `piper` 22,7 · `PIL` 13,9 · `grpc` 11,9.

Ce que le destinataire téléchargera au premier usage du RAG (résolu, non pesé) :
`torch 2.13.0`, `transformers 5.15.0`, `scikit-learn`, `scipy`, `sympy`, `networkx`,
`safetensors`, `regex`, `joblib`, `Jinja2` — 16 paquets.

### 4. Périmètre exact de l'Atelier à retirer

**Backend — 12 routes.** `main.py:323-460` : `GET /workshop/engines`,
`GET /workshop/modules`, `POST /workshop/generate`, `POST /workshop/{id}/edit`,
`GET /workshop/staging/{id}`, `POST /workshop/{id}/validate`,
`POST /workshop/{id}/approve`, `POST /workshop/{id}/reject`, `WS /ws/workshop`.
`modules/settings/router.py:503,520,531` : `POST /settings/test/aider`,
`POST /settings/test/gateway`, `POST /settings/gateway/start`.

**IMPÉRATIF — `core/module_workshop.py` ne peut pas être supprimé du paquet.**
`core/catalogue.py:32-40` en importe sept symboles (`_FILES`, `_backup_existing`,
`_check_module_id`, `_drop_module_routes`, `_frontend_component_path`, `_remount`,
`modules_dir`), et c'est ce qui fait marcher `POST /settings/catalogue/{id}/install` et
`DELETE /settings/modules/{id}` — les deux fonctions que le proche garde. On retire les
routes, pas le module.

| Fichier backend | Sort |
|---|---|
| `core/module_workshop.py` (1727 l.) | **garder** — dépendance de `catalogue.py` |
| `core/module_validate.py`, `core/smoke_runner.py` | retirables — Atelier seul (`module_validate` n'est importé que par `module_workshop` ; `smoke_runner` est lancé en sous-process depuis `module_workshop:1482`) |
| `core/codeagent.py` | **garder** — `runtime.py:60`, `main.py:30`, `modules-catalogue/code/router.py:19`, `catalogue.py:31` |
| `core/module_worker.py` | aucun importeur (chantier CLAUDE.md §7, non câblé) |
| `core/instance.py:86` | bloc de config `atelier` : laisser dans le schéma (`_deep_merge` / `_garder_settings` en dépendent), plus rien ne le lira |
| `modules/_atelier/`, `modules/_staging/`, `modules/_backups/` | hors paquet |

| Fichier frontend | À retirer |
|---|---|
| `components/Workshop.tsx` (942 l.) | l'écran — déjà en chunk séparé au build (`Workshop-*.js`, 26,8 ko), donc le sortir du registre le fait disparaître du bundle |
| `modules/registry.ts:58` | entrée `CORE_DEFS` `{ id: 'workshop', … }` |
| `components/Sidebar.tsx:95-98` | bouton « Atelier » |
| `App.tsx:144` | `'workshop'` en dur dans `visibleIds` |
| `modules/settings/Component.tsx` | Card « Atelier — moteurs » l. 819-960 ; état `engines` / `startingGateway` / `gatewayStartMsg` (183-187) ; `loadEngines()` (276-282) appelé au montage (268) ; `startGateway()` (296-307) ; `ENGINE_LABELS` (20-22) |
| `components/ModuleErrorBoundary.tsx:76-85` | bouton « Corriger dans l'atelier » — sinon il propose au proche un écran qui n'existe pas |

**Bonus pour l'étape D** : le « test à casser exprès » est **déjà satisfait** pour
`PUT /modules/{id}/status` — `set_status` retourne `None` si l'id n'est pas dans
`installed_ids()` (`core/module_registry.py:199`), l'état « installé » étant dérivé du
disque et non stocké. Le seul levier restant est `GET /settings/catalogue`, qui liste
`modules-catalogue/` : la restriction se fait en ne mettant dans ce dossier que les
modules choisis pour la personne.

### 5. `google-generativeai` retiré, `grpcio`/`opentelemetry-exporter-otlp-proto-grpc`/`googleapis-common-protos` purgés — **159,4 Mo à télécharger**

Mesuré le 2026-08-10, en fin de journée. `googleapiclient` (97,9 Mo, le plus gros poste
du point 3 ci-dessus) n'arrivait que par `google-generativeai==0.8.6`, dont l'import
affichait déjà « All support for the `google.generativeai` package has ended ». Rien
d'autre dans `requirements.txt` n'en dépend : l'ajouter à `HORS_PAQUET_PIP` (comme
`sentence-transformers`) fait disparaître tout son arbre transitif de la résolution —
`grpcio-status`, `proto-plus`, `google-ai-generativelanguage`, `google-api-core`,
`google-auth`, `google-api-python-client`. Vérifié dans le paquet construit : aucun de
ces noms n'apparaît plus sous `site-packages/`.

**`grpcio` et `opentelemetry-exporter-otlp-proto-grpc`, eux, restent installés même sans
`google-generativeai`** — chromadb les déclare en dépendances directes et
inconditionnelles (`Requires-Dist: grpcio>=1.58.0`,
`opentelemetry-exporter-otlp-proto-grpc>=1.2.0`), donc `pip` les réinstalle pour le
satisfaire, quoi qu'on retire de `requirements.txt`. Idem pour `googleapis-common-protos` :
`opentelemetry-exporter-otlp-proto-common` (qui reste, chromadb en a besoin pour encoder
traces/métriques) le déclare à son tour. Les trois sont donc **purgés du `site-packages`
après installation** (`PURGE_DISTRIBUTIONS`, `tools/faire_paquet.py`) — par lecture du
`RECORD` de leur `.dist-info`, pas par un simple nom de dossier : `grpcio` installe
`grpc/` (pas `grpcio/`), et `opentelemetry-exporter-otlp-proto-grpc` /
`googleapis-common-protos` installent chacun sous un espace de noms **partagé** avec
d'autres distributions qui restent (`opentelemetry/exporter/otlp/proto/common/`,
`google/protobuf/`) — un retrait par nom de dossier aurait emporté les deux à la fois.

Un seul point d'import casse avec cette purge :
`chromadb/telemetry/opentelemetry/__init__.py` importe `OTLPSpanExporter` de
`opentelemetry.exporter.otlp.proto.grpc.trace_exporter` **au niveau module** — donc dès
`import chromadb`, avant même de construire un client. Elle n'est pourtant jamais
INSTANCIÉE en usage réel : `chroma_otel_granularity` vaut `"none"` par défaut, et
`otel_init()` retourne avant de la construire. D'où `sitecustomize.py`, posé dans
`Lib/site-packages/` (chargé automatiquement par `site` — la même bascule `import site`
déjà nécessaire pour `pip`) : il pré-enregistre un module factice pour ce seul chemin
d'import, avec une classe qui n'existe que pour être importée.

**Vérifié pour de vrai**, sur le Python embeddable construit, pas seulement en lisant le
code : `import chromadb`, `PersistentClient` et `EphemeralClient` — `add`/`query`/`count`
fonctionnent tous les trois sans `grpcio` ni `opentelemetry-exporter-otlp-proto-grpc`
installés. **Contrôle négatif** : retirer `sitecustomize.py` du même paquet fait
effectivement casser `import chromadb` (`ModuleNotFoundError: No module named
'opentelemetry.exporter.otlp.proto.grpc'`, dès `chromadb/__init__.py` ligne 11) — la
preuve que le stub est réellement nécessaire, pas accessoire. `import google.generativeai`
et `import grpc` échouent proprement (`ModuleNotFoundError`), comme voulu.

| | zippé |
|---|---|
| point 3 (`google-generativeai` installé) | 186,6 Mo |
| **ce point (`google-generativeai` exclu, `grpcio`+cluster purgés)** | **159,4 Mo** |

Chiffres non directement comparables : le paquet de ce point inclut un module du
catalogue (`flashcards`), celui du point 3 le cœur seul — l'écart de poids réel
qu'attribuer à cette seule décision est plus proche de 27-30 Mo compressés. Reste que
c'est net, et **conséquence pour l'étape ARM64 (§7, nouveau)** : le mur `grpcio` du
2026-08-10 (aucune wheel `win_arm64`) ne se contourne plus, il s'évapore — `grpcio` n'est
plus installé sur AUCUNE architecture.

---

## §1 — Étapes

### Étape A — Frontend construit, servi par FastAPI

Remplace, **pour la distribution seulement**, les deux processus (Vite + uvicorn) par un
seul. Le mode développement d'Ilyann ne change pas — Vite reste utile pour l'Atelier, qui
n'existe que chez lui.

**Fait en préalable : `src/api.ts` sait maintenant produire des appels relatifs.** L'ancien
`VITE_API_URL?.replace(…) || 'http://localhost:8000'` ne pouvait pas exprimer le mode
« une seule origine » : la chaîne vide étant falsy, le `||` retombait sur le défaut, et un
paquet construit avec une valeur vide contenait quand même `localhost:8000`. Le test
distingue désormais `undefined` (mode dev) de la chaîne vide (mode paquet), et `wsUrl`
prend son schéma sur `window.location.origin` quand `API` est vide. Le réglage est
`VITE_API_URL=/` — cf. étape B pour la raison Windows.

**Pas de route de repli à écrire.** Le front n'a aucun routeur client (ni `react-router`,
ni `history.pushState`, ni `hash` : la navigation est l'état React `activeModule`, persisté
dans localStorage, `App.tsx:24`). Il n'existe **qu'une seule URL, `/`** — donc pas de
catch-all, et c'est heureux : un mount sur `/` capterait aussi les routes des modules
installés **après** le démarrage, puisque `module_workshop._remount` fait un
`app.include_router` qui **ajoute en fin** de `app.router.routes`. Un catch-all posé au
démarrage ferait répondre `index.html` à la place d'un module fraîchement installé depuis
le catalogue — précisément la fonction que le proche garde. Servir explicitement
`/` + `/assets/*` + les fichiers racine de `dist/`, rien de plus.

Point d'attention sécurité : le middleware `_require_api_token` exige un token partout sauf
`/health` et `/pair` (`main.py:81`). La page HTML elle-même doit donc être exemptée, sinon
elle répond 401 avant que le JS ait pu s'appairer. L'exemption doit couvrir exactement les
fichiers statiques, jamais une route d'API.

**Réalisation.** `core.paths.resolve_web_dir()` (`$EPURE_WEB_DIR`, sinon
`<repo>/frontend/dist`) et `main._register_web(app)`, appelé après `_register_routers`.
Éteint sans `index.html` — donc le mode développement d'Ilyann est inchangé. Sont montés :
`/` et chaque fichier de la racine de `dist/` en routes explicites, plus `/_assets` en
`StaticFiles`.

`build.assetsDir = '_assets'` dans `vite.config.ts`, et pas le défaut `assets` : le préfixe
des assets est exempté d'authentification, or un id de module valide est
`[a-z][a-z0-9_]{1,30}` — un module nommé `assets` aurait donc vu ses routes exemptées.
L'underscore initial rend la collision impossible, et reprend la convention déjà en place
côté source (`registry.ts` exclut `./generated/_*/**`).

**Vérification faite le 2026-08-10.** `npm run build` avec `VITE_API_URL=/`, backend sur
127.0.0.1:8000 avec `EPURE_WEB_DIR` sur le `dist/`, puis Chrome headless **avec un profil
neuf** (donc cache et localStorage vides, l'appairage devant se refaire de zéro) :

- l'interface se rend complètement — barre de modules, thème, écran Réglages peuplé de
  vraies données (modèle actif, dossiers de fiches, catalogue), ce qui prouve que
  l'appairage automatique et les appels API relatifs fonctionnent ;
- **le WebSocket s'ouvre** : `WebSocket /ws/chat?token=… [accepted]`, `connection open`
  dans le journal uvicorn. C'est la dernière inconnue du §6 qui tombe — `wsUrl` dérive bien
  son `ws://` de `window.location.origin` quand `API` est vide ;
- sans token : `/`, `/index.html`, `/favicon.svg`, `/icons.svg` et `/_assets/*` répondent
  200 ; `/models`, `/modules`, `/instance/config`, `/workshop/engines` répondent 401, et
  `/_assets/../models` aussi ;
- avec token : `/models` et `/hello/ping` répondent 200, et **`/inconnu` répond 404 et non
  du HTML** — l'absence de catch-all, vue du client.

Couvert par `backend/test_web_statique.py` (10 tests), dont celui qui compte :
`PasDeCatchAllTest` installe une route **après** le montage statique, comme le fait
`_remount`, et échoue si le statique la masque.

#### Deux constats faits en vérifiant l'étape A

- **Le token d'API apparaissait en clair dans le journal uvicorn** —
  `"WebSocket /ws/chat?token=YGdS…" [accepted]`, contre l'IMPÉRATIF de CLAUDE.md §6.
  Préexistant, mais aggravé par l'empaquetage : jusqu'ici le journal restait sur le poste
  de son propriétaire, qui connaît déjà son token ; dans un paquet c'est un fichier sur le
  disque de quelqu'un d'autre, recopié dans un message quand quelque chose ne marche pas.
  **Corrigé** avant l'étape B : `core/logs.py`, filtre de logging sur `uvicorn.access`,
  `uvicorn.error` et la racine. Vérifié dans le vrai journal —
  `"WebSocket /ws/chat?token=***masqué***" [accepted]`, et le token n'apparaît nulle part
  ailleurs. Le passage du token en sous-protocole WebSocket reste la correction de fond,
  et reste à faire un jour : elle ne protégerait pas les journaux déjà écrits.
- **Une installation neuve ouvre sur Réglages, pas sur Chat.** Hors périmètre de ce
  chantier — note ouverte dans `docs/note-premier-ecran.md`.

### Étape B — Constituer un paquet pour un destinataire donné

Un script **côté Ilyann, jamais livré** : prend une liste de modules choisis pour cette
personne, copie le backend (Atelier retiré ou désactivé), le frontend construit, un
`site-packages` pré-installé (Python embeddable 3.12, torch exclu), une configuration
minimale, et zippe le tout. Vit dans un dossier séparé (`tools/` ou dépôt à part), pas
dans ce qui part chez le proche.

Trois contraintes établies par le §0, à respecter par ce script :

- **Construire le front en mode paquet** : `VITE_API_URL=/`, pas la chaîne vide. Sous
  Windows `$env:VITE_API_URL = ''` **supprime** la variable (mesuré : le process enfant la
  voit non définie), donc une chaîne vide ferait silencieusement un paquet en
  `http://localhost:8000` — le bug exact que la correction d'`api.ts` élimine. Contrôle
  après build : `localhost:8000` n'apparaît qu'**une** fois dans `dist/assets/index-*.js`
  (le texte de l'écran d'appairage, `App.tsx:290`), et non deux.
- **Exclure `kubernetes/`** (37,8 Mo). Il n'est importé que par
  `chromadb/segment/impl/distributed/segment_directory.py` — le chemin de déploiement
  distribué — et par les tests de chromadb. Vérifié : après retrait, les 22 imports du
  backend passent et `PersistentClient` (le seul client construit dans le dépôt,
  `core/rag.py:31`) fonctionne, `add`/`query`/`count` compris. Ne pas se contenter du
  `pip install`, qui le tirera toujours : c'est une dépendance déclarée de chromadb.
- **Épingler l'arbre complet, pas seulement `requirements.txt`.** Installer
  `google-generativeai==0.8.6` **rétrograde `protobuf` 7.35.1 → 5.29.6** et fait
  rétro-parcourir au résolveur une quinzaine de versions de `grpcio-status`. Rien n'a
  cassé (22 imports sur 22), mais deux constructions à deux dates ne donneront pas le même
  paquet. Un `pip freeze` du poste de build, versionné à côté du script, ou
  `--require-hashes`.

À noter, sans agir maintenant : `googleapiclient` pèse **97,9 Mo**, soit 17 % du paquet, et
n'arrive que par `google-generativeai==0.8.6`, dont l'import affiche déjà « All support for
the `google.generativeai` package has ended […] switch to the `google.genai` package ».
Migrer ce fournisseur est le plus gros gain de poids disponible, et c'est un chantier à
part.

**Réalisation : `tools/faire_paquet.py`.** Hors de ce qui est livré, comme demandé —
`tools/` n'est pas dans `backend/`, donc rien ne peut l'y faire entrer, et
`test_paquet.py` l'affirme quand même (la façon la plus probable de le casser serait de
déplacer le script dans `backend/` pour qu'il puisse importer `core.*`).

```powershell
python tools\faire_paquet.py --lister-modules
python tools\faire_paquet.py --destinataire sandr --modules flashcards,reviseur
```

Arborescence produite — elle **reproduit celle du dépôt**, `app/` tenant le rôle de
racine :

```
epure-sandr.zip
  python/                  runtime embeddable 3.12 + site-packages
  app/backend/             le code, sans les données ni l'Atelier
  app/backend/modules/<id> les modules choisis, déjà installés
  app/frontend/dist/       l'interface construite en mode paquet
  PAQUET.json              ce qui a été mis dedans, et avec quoi
```

Ce n'est pas cosmétique : ainsi **tous les défauts de `core/paths.py` tombent juste sans
une seule variable d'environnement** — `resolve_web_dir()` trouve `app/frontend/dist`,
`resolve_modules_dir()` trouve `app/backend/modules`, `resolve_data_dir()` crée
`app/backend/memory`. Poser cinq variables dans un lanceur serait cinq occasions d'en
oublier une, et l'oubli se verrait tard.

#### Trois décisions prises en écrivant le script

**1. L'Atelier est désactivé, pas retiré.** Contrainte du §0 : `catalogue.py` importe sept
symboles de `module_workshop.py`, qui importe `module_validate.py` au niveau module.
Supprimer ces fichiers casserait l'écran Réglages du destinataire, pas l'Atelier. Ce qu'on
retire, ce sont les routes et l'écran, par deux interrupteurs :

- `EPURE_ATELIER=0` (backend) → 404 sur `/workshop*`, `/settings/test/*`,
  `/settings/gateway/*`, et fermeture de `/ws/workshop` avant `accept()`. Le 404 est posé
  **avant** le contrôle de token, sinon un 401 révélerait que la route existe.
- `VITE_ATELIER=0` (frontend) → l'Atelier sort du **bundle**, pas seulement de l'écran.
  Mesuré et corrigé en route : avec le drapeau écrit `VITE_ATELIER?.trim() !== '0'`,
  rolldown ne plie plus la constante, la branche morte reste atteignable et un
  `Workshop-*.js` de **26,1 ko contenant le code de l'Atelier** partait quand même, non
  référencé par l'index mais bien sur le disque. Orphelin n'est pas absent. Deux gardes :
  la comparaison reste directe (`test_paquet.py`), et `vite.config.ts` détourne le
  specifier vers un module vide quand le drapeau est à `0`.

**2. `modules-catalogue/` ne part pas.** Installer un module depuis le catalogue écrit un
`Component.tsx` dans les sources du frontend, ce qui suppose un build ; dans un paquet il
n'y a ni `npm` ni sources. Le destinataire peut donc **activer et désactiver** ce qu'il a
reçu (étape D), pas installer autre chose. Sans catalogue livré,
`GET /settings/catalogue` renvoie une liste vide et le bouton n'apparaît pas —
l'incapacité est honnête plutôt que cassée.

**3. Le `generated/` du poste de build est mis de côté pendant le build.** `registry.ts`
découvre les modules par `import.meta.glob('./generated/**/*.tsx')` : **tout** ce qui
traîne dans cet arbre entre dans le bundle. Sans filtre, le paquet de quelqu'un
contiendrait le code source des modules faits pour quelqu'un d'autre — ce que l'étape D
interdit. Le vrai dossier est écarté par un `rename`, reconstruit avec les seuls modules
choisis (depuis le catalogue, pas depuis l'arbre installé), et restauré dans un `finally`.
Si une garde résiduelle traîne, le script **refuse** au lieu d'écraser : les composants
installés d'Ilyann ne se rattrapent pas.

#### Ce que les tests couvrent, et pourquoi eux

`backend/test_paquet.py` (27 tests). Le sujet n'est pas « le script marche » mais **ce qui
ne doit pas sortir du poste** : un paquet est une archive envoyée à quelqu'un, et s'il
emporte `backend/.env` il emporte toutes les clés cloud, sans reprise possible. D'où deux
niveaux : la règle d'exclusion interrogée comme fonction pure, **et** la copie réelle de
`backend/` inspectée après coup — une règle correcte peut être contournée par un parcours
qui ne la lui passe pas au bon chemin relatif.

Trois défauts trouvés par ces tests avant tout usage :

- `modules/history/` — le module core Historique — **disparaissait du paquet**, parce que
  `history` est aussi le nom du dossier de données `backend/history/` et que l'exclusion
  s'appliquait à n'importe quelle profondeur. Les exclusions de données sont désormais
  ancrées à la racine de `backend/`. Le paquet se construisait sans erreur ; le
  destinataire n'avait simplement pas d'historique.
- `.env.example` était exclu par une assertion trop large. Il **doit** partir : il ne
  contient que des clés vides et c'est ce qui explique comment renseigner les siennes. Un
  test vérifie maintenant qu'aucune valeur n'y est renseignée.
- un test de non-fuite qui ne peut pas échouer est plus dangereux que pas de test — il dit
  « aucune clé ne part » alors qu'il n'y avait aucune clé. Le test vérifie donc d'abord que
  `backend/.env` existe, et se déclare `skip` sinon (cas de la CI).

### Étape C — Installeur minimal pour le proche — **faite le 2026-08-23**

`tools/installer-epure.ps1` + `tools/Installer-Epure.cmd`. Le destinataire reçoit
**trois fichiers dans un même dossier** et double-clique le `.cmd` :

```
epure-<destinataire>.zip     l'archive produite par tools/faire_paquet.py
installer-epure.ps1          la logique
Installer-Epure.cmd          ce qu'il double-clique
```

Les deux fichiers d'installation sont désormais **copiés à côté de l'archive à chaque
assemblage** (`faire_paquet.poser_installeur`). Ce n'est pas une commodité : « à
joindre au moment de l'envoi » est exactement la classe d'oubli qui a fait que le
lanceur du paquet, pourtant écrit à la main plusieurs fois, n'a existé dans **aucun**
paquet assemblé jusqu'ici. Un fichier qu'il faut penser à produire est un fichier qui
manque.

#### Pourquoi PowerShell, et pourquoi deux fichiers

PowerShell 5.1 est livré avec Windows 10 et 11 : c'est la seule chose dont on soit
sûr qu'elle est déjà là. Un `.exe` demanderait un toolchain au build et se ferait
arrêter par SmartScreen faute de signature — le même problème que celui décrit plus
bas pour les `.pyd`, mais sur le premier fichier que le destinataire doit lancer. Un
`.cmd` seul ne sait ni dézipper ni poser un raccourci.

Le `.cmd` d'amorçage existe pour deux raisons distinctes, et il faut les deux : un
`.ps1` double-cliqué **s'ouvre dans le Bloc-notes** au lieu de s'exécuter, et
l'ExecutionPolicy par défaut **refuse** un script non signé venu d'internet.
`-ExecutionPolicy Bypass` ne change rien sur la machine : l'option ne vaut que pour ce
processus.

#### Où ça s'installe, et pourquoi pas ailleurs

`%LOCALAPPDATA%\Epure` par défaut (`-Cible` pour en changer). Bureau et Documents
sont **redirigés vers OneDrive** sur beaucoup de postes — vérifié sur celui-ci :
`[Environment]::GetFolderPath('Desktop')` rend `<profil>\OneDrive\Desktop`. Une
installation là-dedans se ferait synchroniser : 132 Mo et 7 410 fichiers dont un
`python.exe`, avec des verrous de fichiers pendant l'exécution et des copies « (2) »
à la moindre réinstallation. `LOCALAPPDATA` n'est jamais redirigé et ne demande aucun
droit administrateur.

#### Le lanceur, généré et non plus écrit à la main

Deux fichiers, régénérés à chaque installation (ce ne sont pas des données) :

| Fichier | Rôle |
|---|---|
| `Epure.cmd` | une ligne : `start "" python\pythonw.exe demarrer.py`. Rend le lanceur double-cliquable depuis l'explorateur. |
| `demarrer.py` | tout ce qui se raisonne : attendre le port, ouvrir le navigateur, journaliser. |

`pythonw.exe` et non `python.exe` : pythonw appartient au sous-système GUI, il n'a
pas de console. **Et tout découle de là** — `sys.stdout` et `sys.stderr` valent
`None`, donc un simple `print()` lèverait `AttributeError` et une traceback
n'existerait nulle part. `demarrer.py` redirige donc les deux flux vers `epure.log`
dès sa première ligne utile, et passe `log_config=None` à uvicorn pour qu'il
n'installe pas ses propres handlers sur un stdout absent : ses loggers remontent
alors à la racine, donc dans le même fichier. Le filtre de `core/logs.py`, posé par
`main.py` sur `uvicorn.access`, continue de masquer le token qui voyage en query
param des WebSockets.

Le port (8000, loopback) est testé **avant** de démarrer uvicorn, par `GET /health`
et pas par un simple `connect` : avec un raccourci sur le Bureau, double-lancer est
le cas normal, et deux uvicorn sur le même port donnent une erreur de bind
invisible. Si Épure répond déjà, on ouvre seulement le navigateur. Le test porte sur
`/health` parce que le port 8000 est banal — conclure « Épure tourne » parce que
quelque chose écoute ouvrirait le navigateur sur le programme de quelqu'un d'autre.

Le raccourci du Bureau (`Épure.lnk`) vise **`pythonw.exe` directement**, pas
`Epure.cmd` : un raccourci vers un `.cmd` ouvre une console le temps du `start`, donc
un clignotement noir à chaque lancement.

#### Réinstallation : la règle, et pourquoi elle est correcte

**On écrit tout ce que l'archive contient, on ne supprime jamais rien d'autre.**

Cette règle suffit, et elle est correcte pour une raison qui n'est pas dans
l'installeur : l'archive **ne contient aucune donnée du destinataire**.
`tools/faire_paquet.py` exclut `memory/`, `history/`, `vector_db/`, `chroma_db/`,
`doc_uploads/` et `piper_models/` à la racine de `backend/` (`EXCLUS_RACINE`) ainsi
que le `.env` (`EXCLUS_FICHIERS`), et `backend/test_paquet.py` le vérifie à chaque
commit. Les données survivent donc parce qu'elles sont **absentes de l'archive**, pas
parce qu'une liste de protection les nomme — ce qui est la seule forme de garantie
qui ne se périme pas quand on ajoute un dossier de données.

Ce que « le reste » couvre exactement, d'après l'archive réellement produite
(mesuré sur `dist-paquets/epure-sandr.zip`, 7 410 entrées) :

| Dans l'archive → **écrasé** | Contenu réel |
|---|---|
| `PAQUET.json` | 1 fichier : destinataire, modules, arch, voix, gel des versions |
| `app/backend/` | 41 fichiers : `main.py`, `core/*.py` (25), `modules/<id>/{manifest.json,router.py}`, `config.yaml`, `requirements.txt`, `.env`, `.env.example`, `Dockerfile`, `.dockerignore` |
| `app/frontend/dist/` | 144 fichiers : `index.html` + `_assets/` (JS/CSS horodatés par un hash) |
| `python/` | 7 224 fichiers : runtime embeddable 3.12 + `Lib/site-packages` |

| Hors archive → **jamais touché** | Pourquoi |
|---|---|
| `app/backend/memory/` | profil, config d'instance, token d'API, contexte de session |
| `app/backend/history/` | conversations |
| `app/backend/vector_db/` | index vectoriel — **et le texte** des fiches et PDF indexés |
| `app/backend/chroma_db/` | ancien index, s'il existe encore |
| `app/backend/doc_uploads/` | PDF déposés dans l'interface |
| `app/backend/piper_models/` | `.onnx` de la voix, 76 Mo téléchargés au premier usage |
| `app/workspace/` | `resolve_workspace()` → `<racine>/workspace`, et `app/` tient le rôle de racine |
| `app/data/fiches/` | `resolve_fiches_dir()` → `<racine>/data/fiches` : les fiches du destinataire |
| tout le reste | y compris le `torch` que l'application installe elle-même dans `python/Lib/site-packages` |

**Deux exceptions**, toutes deux volontaires :

1. **`app/backend/.env` est dans l'archive** (`faire_paquet.py` l'écrit pour éteindre
   l'Atelier côté serveur, cf. écart 5 plus bas) **et** c'est le fichier où
   `PUT /settings/api-keys` écrit les clés du destinataire. L'écraser lui coûterait
   toutes ses clés — et « installer la mise à jour » est précisément le geste après
   lequel personne ne va vérifier ses clés. Il n'est donc écrit que s'il est absent.
   S'il existe, l'installeur vérifie seulement qu'il porte encore `EPURE_ATELIER=0`
   et ajoute la ligne si elle manque : sans elle, `main.py` reprend son défaut
   (`"1"`, donc **actif**) et les routes `/workshop*` redeviennent joignables alors
   que l'écran est absent du bundle. C'est l'écart 5, mais sur le chemin de la mise à
   jour, où personne ne l'avait cherché. Et s'il n'y en a **aucun** — ni dans
   l'archive ni sur le disque — l'installeur en crée un : `faire_paquet.py` n'écrit
   ce fichier que depuis le 2026-08-22, donc l'archive déjà produite pour sandr n'en
   contient pas, et sans cette branche l'Atelier serait joignable ou non selon le
   millésime du zip. Un invariant de sécurité ne doit pas dépendre de l'âge de
   l'archive.
2. **`app/frontend/dist` est vidé avant recopie.** C'est le seul dossier purement
   généré dont les noms de fichiers changent à chaque build (`index-<hash>.js`) :
   une simple superposition les empilerait à chaque mise à jour, sans que rien ne le
   signale puisque `index.html` ne référence que le dernier. `python/` est aussi
   généré mais n'est **pas** traité ainsi — l'application y installe `torch` et
   `sentence-transformers` au premier usage de la recherche documentaire (~2 Go, cf.
   écarts 2 et 3), et le vider les ferait retélécharger.

**La promesse est vérifiée à l'exécution, pas seulement documentée.** L'installeur
prend un instantané (taille + date) de tout fichier présent sous les emplacements
ci-dessus **avant** de déployer, et le recompare **après**. Un seul fichier modifié
ou disparu et il s'arrête sur un message rouge nommant les fichiers. Taille et date
plutôt qu'un hash : un `vector_db` peut peser des centaines de Mo, et ce qu'on
cherche est un écrasement, pas une corruption d'octets. `EXCLUS_RACINE` et la liste
surveillée sont tenues alignées par `backend/test_installeur.py` — ajouter un dossier
de données sans le déclarer à l'installeur ne casserait rien de visible, ça cesserait
juste de le surveiller.

L'ordre compte, et un test l'a montré tout de suite : la vérification porte sur le
**déploiement**, donc elle passe **avant** la remise de `EPURE_ATELIER=0`. Dans le
premier jet elle passait après, et le garde-fou échouait sur son propre travail —
« 1 fichier de données touché : `.env` ».

**Limite déclarée** : une mise à jour **ajoute et remplace, elle ne retire pas**. Un
fichier retiré du paquet entre deux versions (`migrer_vectoriel.py`, sorti par
`EXCLUS_MAINTENANCE`) reste sur le disque du destinataire, inerte. Aucune règle ne
distingue de façon sûre « fichier retiré du paquet » de « fichier ajouté par le
destinataire », et se tromper dans ce sens-là coûte des données. Affirmé par un test,
pour que ce soit une décision et non une surprise.

#### Ce que l'installeur refuse plutôt que de deviner

- **Une archive qui n'est pas un paquet Épure** (pas de `PAQUET.json`, pas de
  `app/backend/main.py`) : refus avant d'écrire un seul octet. Sans ce contrôle,
  n'importe quel zip posé à côté du script se déverserait dans
  `%LOCALAPPDATA%\Epure` et l'installation aurait l'air réussie.
- **Plusieurs `epure-*.zip` dans le dossier** : refus avec la liste. Installer la
  mauvaise version est invisible jusqu'au premier bug qu'on cherchera dans le code.
- **Une instance en cours d'exécution** (détectée par `GET /health`) : refus, parce
  que les fichiers sont verrouillés et qu'une copie à moitié écrite est pire qu'une
  copie refusée.
- **Une entrée d'archive qui sortirait du dossier d'installation** (zip slip). Le
  confinement se fait par canonicalisation puis comparaison **avec le séparateur
  final** — sans lui, un dossier frère `<racine>-autre` passerait pour un enfant de
  `<racine>`. Même règle que `core/paths.py` (CLAUDE.md §3.5). L'archive vient de
  notre propre script, mais un extracteur qui fait confiance à ses entrées est un
  extracteur cassé le jour où elle vient d'ailleurs.

#### Ce qui n'est pas fait, et qu'il ne faut pas croire fait

Le plan de cette étape prévoyait aussi « installe Ollama s'il est absent, tire le
modèle ». **Ce n'est pas dans l'installeur.** Ces deux actions demandent plusieurs Go
de téléchargement et, selon le chemin choisi, des droits administrateur ; elles n'ont
rien à voir avec le paquet, et un installeur qui les tente devient un installeur qui
échoue au milieu. Épure démarre et sert son interface sans Ollama : les modèles
locaux sont annoncés indisponibles, ce qui est un état cohérent — et depuis le
2026-08-23 les modèles cloud sans clé n'apparaissent tout simplement pas. Le
destinataire installe Ollama lui-même s'il veut du local, ce qui est un choix
séparé.

Non fait non plus : la mesure demandée par le plan — *sur une machine où rien n'est
installé, le temps et le nombre d'étapes réellement franchies*. Elle attend le
premier vrai destinataire. Ce qui est mesuré ici, c'est le comportement de
l'installeur sur ce poste, en première installation comme en mise à jour
(`backend/test_installeur.py`, 28 tests, dont 21 qui lancent vraiment PowerShell sur
une archive fabriquée).

#### Échauffement Smart App Control — à traiter ici, pas ailleurs

Sur un Windows 11 où **Smart App Control est appliqué**, le premier chargement d'une DLL
non signée fraîchement dézippée dans un dossier utilisateur peut être **bloqué**, puis
autorisé quelques minutes plus tard sans que rien n'ait changé sur le disque. Mesuré le
2026-08-10 pendant le test du §0, sur ce poste (`VerifiedAndReputablePolicyState = 1`,
`CodeIntegrityPolicyEnforcementStatus = 2`) :

```
ImportError: DLL load failed while importing stream:
Une stratégie de contrôle d'application a bloqué ce fichier.
```

Journal `Microsoft-Windows-CodeIntegrity/Operational`, **événement 3077** :

```
Code Integrity determined that a process (…\embed\python.exe) attempted to load
…\embed\Lib\site-packages\av\video\stream.pyd that did not meet the Enterprise
signing level requirements or violated code integrity policy
(Policy ID:{0283ac0f-fff1-49ae-ada1-8a933130cad6})
```

La même commande, sur les mêmes fichiers au même chemin, a réussi ~2 minutes plus tard :
c'est une évaluation de réputation en cours, pas un refus définitif. Ce n'est **pas** une
limite du Python embeddable — mais c'est exactement la forme du paquet livré (des `.pyd`
non signés dézippés dans un dossier utilisateur), donc un échec au premier lancement chez
le proche est un risque réel, **intermittent**, et donc le pire à diagnostiquer à
distance : au second essai il aura disparu.

Aucun correctif par le code. Trois options, à trancher à l'étape C et pas avant :

1. **Annoncer et relancer** — le moins cher : l'installeur dit qu'un premier lancement
   peut échouer et qu'il faut relancer. Le journal du tray (`epure_tray.log`) doit alors
   contenir le message ci-dessus en clair, sinon le proche voit une application qui ne
   démarre pas sans savoir pourquoi.
2. **Échauffer à l'installation** — l'installeur importe une fois chaque module natif et
   retente sur échec, pour que le blocage tombe pendant l'installation (où l'attente est
   attendue) plutôt qu'au premier usage.
3. **Signer les binaires** — la seule vraie solution, hors budget.

**Tranché le 2026-08-23 : option 2, doublée de l'option 1.** L'installeur importe une
fois chaque extension compilée du paquet (`numpy`, `sqlite3`, `pydantic_core`,
`tokenizers`, `onnxruntime`, `lxml`, et `av`/`ctranslate2` quand la voix est livrée),
juste après la copie. En cas d'échec il attend 20 s et retente une fois ; si ça
échoue encore, il l'écrit noir sur blanc et dit de relancer dans quelques minutes.
Trois raisons de choisir ainsi plutôt que l'option 1 seule :

- le blocage tombe **pendant l'installation**, là où l'attente est attendue, et non
  au premier lancement où l'application a l'air cassée ;
- la liste des modules est décidée par `find_spec`, pas par une supposition
  d'architecture : le paquet ARM64 n'a ni `av` ni `ctranslate2`, et un import qui
  échoue parce que le module est absent n'est pas un blocage de stratégie ;
- l'échec reste **visible** dans `installation.log`, ce que l'option 1 seule ne
  garantissait pas — c'était sa faiblesse : un message d'avertissement lu trois
  semaines plus tôt ne diagnostique rien.

L'option 3 reste la seule vraie solution et reste hors budget. `-SansEchauffement`
existe pour les tests, qui n'ont aucune DLL à charger.

Ce qui n'est pas mesuré : si le blocage se reproduit sur une machine **jamais** exposée à
ces fichiers (ici, `pip` venait de les écrire, et le poste avait déjà vu ce genre de DLL).
À vérifier sur la machine du premier destinataire.

#### Trois écarts découverts en écrivant la procédure d'installation (2026-08-22) — **les trois corrigés au 2026-08-23**

Relecture du chemin réel du destinataire, code en main, avant d'écrire l'installeur.
Trois choses que le paquet ne faisait pas alors que le plan et les docstrings les
donnaient pour acquises. Elles sont ici et pas dans un fil de discussion parce que
chacune se lit comme réglée tant qu'on ne suit pas la commande jusqu'au bout.

Ce qu'elles ont en commun, et qui vaut plus que chacune d'elles : **un docstring qui
décrit une intention se lit exactement comme un docstring qui décrit du code.** L'écart 5
annonçait un Atelier éteint que rien n'éteignait ; l'écart 2 annonçait une installation
« au premier usage » que rien n'installait. Dans les deux cas la phrase était juste sur
l'intention et fausse sur l'instance, et dans les deux cas ça ne se voyait qu'en suivant
la commande jusqu'au bout — pas en relisant.

##### Écart 5 — l'Atelier restait joignable — **corrigé par ce commit**

`VITE_ATELIER=0` sort l'Atelier du **bundle**, donc de l'écran. Les **routes**
(`/workshop*`, `/ws/workshop`, `/settings/test/`, `/settings/gateway/`) dépendent d'une
autre bascule, lue au démarrage :

```python
_ATELIER_ACTIF = os.environ.get("EPURE_ATELIER", "1").strip() != "0"   # main.py
```

Défaut **`"1"`**. Or `assembler()` n'écrivait que `PAQUET.json`, le paquet ne contient
aucun lanceur, et rien ne posait la variable. Dans tout paquet livré jusqu'ici, l'Atelier
était donc **invisible et joignable en HTTP** — un `curl` sur `/workshop/status` répondait.
Aggravant : `PAQUET.json` portait `"atelier": false` **en dur**, une métadonnée exacte sur
l'intention de celui qui assemble et fausse sur l'instance qui démarre.

Corrigé en écrivant `app/backend/.env` avec `EPURE_ATELIER=0` (`ENV_PAQUET`,
`ecrire_env()`), et en **relisant ce fichier** pour renseigner `PAQUET.json`
(`atelier_actif_selon()`), qui rejoue la règle de `main.py` au lieu de la supposer.

Un `.env` plutôt qu'un lanceur : `core/paths.py` fait `load_dotenv(_BACKEND_DIR/".env")`
à l'import et `main.py` importe `core.admin` — donc `core.paths` — **avant** de lire
`EPURE_ATELIER`. Le fichier est honoré quelle que soit la façon dont uvicorn est lancé ;
un lanceur ne couvrirait que sa propre invocation. Il n'y a par ailleurs pas de conflit
avec `EXCLUS_FICHIERS`, qui interdit de **copier** le `.env` d'Ilyann : on n'en copie
aucun, on en **écrit** un dont le contenu est connu et sans secret.

Ce qu'il a fallu pour que les tests sachent dire non : `backend/test_paquet.py` vérifie la
ligne, refuse toute autre affectation dans le fichier livré, et surtout fait **diverger**
le `.env` et le manifeste (`ENV_PAQUET` monkeypatché sur un contenu qui n'éteint rien →
`PAQUET.json` doit annoncer `true`). Mesuré : sans ce dernier test, remettre
`"atelier": False` en dur laissait les 43 autres verts — dans le cas nominal, « constater »
et « déclarer » sont indiscernables.

##### Écart 2 — rien ne téléchargeait torch au premier usage — **corrigé le 2026-08-23**

Décision 3 du docstring de `tools/faire_paquet.py` disait que `torch` « s'installe au
premier usage du RAG ». **Aucun chemin de code ne faisait ça.** `VectorStore.__init__`
faisait

```python
from sentence_transformers import SentenceTransformer   # core/vector_store.py
```

et levait un `ImportError` s'il manquait ; aucun appel à `pip` n'existait côté
application. Le premier document chargé produisait donc une **erreur**, pas un
téléchargement. Au démarrage, `_warmup` échouait proprement (`Préchauffage RAG échoué` en
trace, attrapée), l'app servait le reste : la dégradation était correcte, c'est bien
l'installation qui n'avait jamais été écrite.

Et cette erreur ne restait pas dans les logs. Le corps du 500 —
`{"detail": "Erreur interne du serveur", "type": "ImportError"}` — n'a pas de champ
`files`, ce que le panneau fichiers du module Docs lisait quand même : `availableFiles`
passait à `undefined` et l'ouverture du panneau levait « Cannot read properties of
undefined (reading 'length') » dans un chunk minifié. La normalisation côté client a été
faite séparément (`ModuleBar.test.tsx`, CLAUDE.md §8), mais elle ne rendait le panneau
que **vide et silencieux** : correct, et incompréhensible pour son utilisateur.

##### Écart 3 — et `pip` était purgé, donc rien ne pouvait le rattraper — **corrigé le 2026-08-23**

`PURGE_SITE_PACKAGES = ("pip", "setuptools", "pkg_resources")` : `python.exe -m pip`
n'existait pas chez le destinataire. Les deux exclusions se composaient sans que personne
ne l'ait décidé — `HORS_PAQUET_PIP` reporte torch au premier usage,
`PURGE_SITE_PACKAGES` retirait l'outil qui seul pouvait l'installer. Chacune est
défendable isolément ; leur produit ne l'était pas.

##### Ce qui a été fait : l'option 2, l'installation par l'application

Ce document présentait deux options — garder une procédure manuelle documentée, ou
installer automatiquement — et disait de ne pas trancher avant un essai ARM64. **C'est
l'option 2 qui a été retenue, et sans attendre cet essai.** La raison de ne plus
attendre : l'essai ARM64 devait dire si `get-pip.py` se comporte bien sur un Python
embeddable ARM64. Cette question n'existe plus, puisque `pip` n'est plus retiré du tout —
il n'y a plus rien à rebootstrapper. La mesure attendue portait sur un obstacle que la
correction supprime.

Ce qui bascule, en trois pièces :

| Pièce | Effet |
|---|---|
| **`PURGE_SITE_PACKAGES = ()`** | `pip` et `setuptools` restent dans le paquet. Coût mesuré : **15,2 Mo** (10,1 + 5,1), soit 0,7 % des ~2 Go de torch qu'ils servent à installer. `pkg_resources` part avec eux : c'est un module DE setuptools, le purger seul livrerait un setuptools amputé. |
| **`backend/core/embedding_install.py`** | `VectorStore.__init__` appelle `exiger_pile()` au lieu d'importer aveuglément. Pile absente → un thread lance `pip install torch --index-url https://download.pytorch.org/whl/cpu` puis `pip install sentence-transformers==5.5.1`, **dans cet ordre**, et l'appelant reçoit `EmbeddingIndisponible` porteuse d'un état. |
| **`GET /rag/capabilities`** + `frontend/src/recherche.ts` | L'état (`absent` / `en_cours` / `prêt` / `échec` + `cause`) est exposé sur le modèle de `/voice/capabilities`, et le panneau fichiers l'affiche : « préparation du moteur de recherche documentaire — environ 2 Go, quelques minutes, connexion réseau nécessaire », puis se remplit tout seul. |

Les points de conception qui ne se devinent pas, et qui sont chacun la réponse à un piège
précis :

- **L'ordre `torch` → `sentence-transformers` et l'index PyTorch sont maintenant dans du
  code**, donc testables (`test_embedding_install.py`). C'était une consigne en prose dans
  `requirements.txt` ; une inversion ne se serait vue que sur une machine ARM64, où PyPI
  ne publie aucune wheel `win_arm64` pour torch.
- **Une seule tentative automatique par process.** L'ouverture du panneau fichiers
  déclenche `GET /rag/files` et `GET /rag/capabilities` presque en même temps ; sans garde,
  chacun lancerait son `pip install torch` sur le même `site-packages`. Un verrou et un
  drapeau `_tentee` s'en chargent, et douze appels simultanés sont testés.
- **Un échec ne se relance pas tout seul**, d'où un `POST /rag/install` et un bouton
  « Réessayer ». La cause la plus probable — pas de réseau — se corrige en dehors de
  l'application, et l'utilisateur est le seul à savoir quand.
- **« Pas de réseau » et « pip a échoué » sont deux verdicts distincts**, mesurés et non
  devinés : une sonde HEAD sur l'index PyTorch **avant** de lancer `pip`, où une réponse
  HTTP — même 403 — compte comme réseau présent. La sortie de `pip` est relue ensuite
  comme signal secondaire, pour la coupure qui arrive pendant les 2 Go.
- **L'état persisté ne porte que des verdicts terminaux.** Un `en_cours` sur le disque
  survivrait au process qui l'a écrit et deviendrait un mensonge que rien ne peut réfuter.
  Le fichier (`memory/embedding_install.json`, via `core/jsonstore.py`) est écrit **avant**
  que l'état mémoire change : l'ordre inverse laisse une fenêtre — observée, pas supposée —
  où l'application annonce « échec réseau » alors qu'un redémarrage immédiat repartirait de
  « absent ».
- **Rien ne se télécharge au démarrage.** `core/runtime.py::_warmup` ne préchauffe plus le
  RAG quand la pile manque : résoudre le proxy y lancerait 2 Go sur la connexion du
  destinataire avant qu'il ait ouvert quoi que ce soit. L'installation part au premier
  appel qui a réellement besoin du moteur.
- **`EPURE_EMBEDDING_AUTOINSTALL=0`** coupe l'installation automatique. Deux besoins
  distincts : une instance sur connexion facturée, et la suite de tests — le job `backend`
  de la CI n'installe ni torch ni sentence-transformers, donc sans cette variable
  (posée par `backend/_test_env.py`) le premier test touchant le RAG téléchargerait 2 Go
  sur le runner. La variable est relue avant **chaque** commande, pas seulement au
  déclenchement : un interrupteur qui n'arrête pas ce qui est déjà parti ne tient que la
  moitié de sa promesse.

La procédure manuelle est donc retirée de ce document plutôt qu'archivée : elle décrivait
un rattrapage pour un `pip` qui n'est plus purgé, et la laisser lisible ferait
rebootstrapper `pip` par-dessus celui du paquet.

### Étape D — Réglages du proche : catalogue restreint

L'écran Réglages n'affiche et ne permet d'activer/désactiver que les modules présents
dans **son** paquet. Deux personnes avec des paquets différents ne doivent voir aucune
trace l'une de l'autre.

Test à casser exprès : vérifier qu'un module absent du paquet n'apparaît nulle part,
même par un appel API direct à l'endpoint de statut.

### Étape E — Documentation

`docs/partage.md` et `docs/installation-chez-un-proche.md` sont à réécrire en
profondeur : git et winget côté proche disparaissent entièrement du parcours.

---

## §5 — Hors périmètre, et pourquoi

**Exécutable unique (PyInstaller/Nuitka).** Écarté au profit d'un dossier autonome +
Python embeddable : moins de risques d'imports cachés cassés silencieusement sur une
stack avec extensions natives.

**Build ARM64.** Reporté tant qu'aucun proche confirmé n'est sur cette architecture
au-delà de sandr. À construire à la demande, sur une machine ARM empruntée. Ce qui
bloquait CE build spécifiquement (`grpcio`, aucune wheel `win_arm64`) a disparu avec §0.5
— `grpcio` n'est plus installé sur aucune architecture. Ce qui reste à vérifier sur du
matériel ARM64 réel : les autres extensions natives du paquet. Cf. §7.

**macOS et Linux pour les proches.** Non demandé à ce stade.

**Mécanisme de mise à jour automatique.** Le proche n'a plus de dépôt git. Une mise à
jour est une nouvelle archive renvoyée par Ilyann. Ne jamais écraser au passage : `.env`,
`memory/`, `chroma_db/`, `workspace/` — même règle que `install.ps1` aujourd'hui.

---

## §6 — Ce qui n'a pas été vérifié

Les trois premiers points de cette liste ont été mesurés le 2026-08-10 (cf. §0) et sont
retirés d'ici. Ce qui reste :

- **Le comportement de Smart App Control sur une machine vierge** — le blocage observé
  ici était transitoire (étape C), mais rien ne dit ce qu'il donne sur un poste qui n'a
  jamais vu ces binaires.
- **`win_arm64`** — tout le §0 a été mesuré sur x64. `chroma-hnswlib` et `grpcio` ont
  disparu de l'arbre installé (§0.2, §0.5) : les deux blocages *connus* pour cette
  architecture n'ont donc plus lieu d'être. Mais rien de tout ça n'a encore tourné sur du
  matériel ARM64 réel — cf. §7, qui documente ce point précisément et ce qui reste
  à construire dessus.
- **Le premier lancement du RAG chez le proche** — l'installation à la demande de torch
  (16 paquets, dont `torch 2.13.0` et `transformers 5.15.0`) n'a jamais été exécutée
  depuis un Python embeddable, seulement résolue en `--dry-run`.
- Le temps qu'Ilyann devra consacrer à reconstituer un paquet par destinataire — workflow
  manuel acceptable pour deux ou trois proches, à revoir si ça grandit.

---

## §7 — Profil ARM64 : ce qui est résolu, ce qui reste à mesurer sur du matériel réel

Écrit le 2026-08-10, après §0.5. Distingue ce qui a été réglé **par construction** (donc
vrai sur toute architecture, x64 compris — ce n'est pas une garantie ARM64 spécifique) de
ce qui n'a encore été mesuré **sur aucune machine ARM64 réelle**.

### Résolu par construction (§0.5), pas par une vérification ARM64

**Gemini est indisponible dans le paquet — pas parce qu'aucune wheel `grpcio` n'existe
pour `win_arm64`, mais parce que `google-generativeai` (et tout ce qu'il tire :
`grpcio`, `grpcio-status`, `opentelemetry-exporter-otlp-proto-grpc`,
`googleapis-common-protos`, `google-ai-generativelanguage`, `google-api-core`,
`google-auth`, `googleapiclient`) n'est plus installé du tout, sur AUCUNE architecture.**
Le mur `win_arm64` du 2026-08-10 (§0, en tête de ce document) est devenu sans objet : il
ne s'agit plus de trouver une wheel qui n'existe pas, il s'agit d'un fournisseur retiré du
paquet par décision, avant même de se poser la question de l'architecture.

Côté proche, ça se traduit par un message clair et non par un plantage : si l'interface
propose encore un modèle `gemini:…` dans la liste (le catalogue de modèles n'a pas été
filtré, cf. ce qui reste à faire ci-dessous) et que quelqu'un le sélectionne,
`core/llm.py` intercepte l'absence du paquet **à l'usage réel**, pas seulement à
l'import — la ligne `import google.generativeai as genai` est dans le corps de
`_stream_gemini`/`_generate_gemini`, donc l'exception ne se lève qu'au premier tour de
boucle du générateur, quand la requête est effectivement lancée. Message renvoyé au
client : `"Package 'google-generativeai' non installé"` (stream) ou
`"[Erreur: google-generativeai non installé]"` (non-stream) — dans les deux cas une
phrase, jamais une trace Python. Vérifié pour de vrai (pas seulement en lisant le code) :
`sys.modules['google.generativeai'] = None` pour simuler l'absence, puis consommation
réelle du générateur — `RuntimeError` propre côté serveur, `str(exc)` propre côté SSE
(`modules/chat/router.py` : `logger.exception(...)` en interne, `{"error": str(exc)}` au
client). Aucun appel réseau n'est fait avant l'échec : l'`ImportError` précède la
vérification de la clé API.

**Tous les autres fournisseurs cloud (OpenAI, Groq, Cerebras, Mistral, NVIDIA,
DeepSeek) restent intacts, sur ARM64 comme ailleurs.** Ils passent tous par
`_OPENAI_COMPAT` (`core/llm.py:63-68`) : un seul client HTTP (le paquet `openai`, lui-même
posé sur `httpx`), pointé vers l'URL de chacun. Aucun de ces deux paquets ne compile de
code natif ni ne dépend d'une architecture — la même wheel `py3-none-any` sert x64 et
ARM64. Rien à vérifier de spécifique à ARM64 ici : c'est HTTP, ça marche partout où
Python tourne. Ollama (le fournisseur local par défaut) est dans le même cas côté client
Python (`ollama` — HTTP pur vers un serveur local) ; le binaire serveur Ollama lui-même
est hors du périmètre de ce document — il n'est pas dans `site-packages`, et son
existence en build `win-arm64` est une question distincte, à vérifier séparément (`ollama
--version` sur la machine cible, pas quelque chose que `faire_paquet.py` installe ou
embarque).

### Non résolu — à mesurer seulement sur du matériel ARM64 réel (§5, §6)

Retirer `grpcio` fait tomber le seul blocage *identifié* du 2026-08-10, mais chromadb et
le pipeline audio embarquent d'autres extensions natives dont la disponibilité en wheel
`win_arm64` n'a **jamais été vérifiée**, dans un sens ou dans l'autre :

| Paquet | Rôle | Risque ARM64 |
|---|---|---|
| `chromadb_rust_bindings` | index ANN de chromadb (a remplacé `chroma-hnswlib`, §0.2) | binding Rust précompilé — inconnu si publié pour `win_arm64` |
| `ctranslate2` | moteur d'inférence de `faster-whisper` | historiquement le paquet le plus restrictif en architectures supportées de toute la chaîne |
| `onnxruntime` | modèles ONNX (embeddings, etc.) | publie des wheels ARM64 sur d'autres OS ; non vérifié pour `win_arm64` précisément |
| `av` (+ `av.libs`) | décodage audio pour `faster-whisper` | bundle des bibliothèques FFmpeg précompilées — même famille de risque que `ctranslate2` |
| `numpy` (+ `numpy.libs`) | calcul numérique, dépendance de tout ce qui précède | wheels ARM64 existent largement ailleurs ; à confirmer pour `win_arm64` |
| `Pillow` | images | même remarque que `numpy` |
| `piper-tts` | synthèse vocale (`core/runtime.py` — `_LazyEngine piper`) | binding natif, non vérifié |

**La seule façon de trancher est celle déjà actée au §5 : construire *sur* une machine
ARM64.** Rien dans ce qui a été vérifié ce soir (§0.5, x64 exclusivement) ne permet de
prédire si `pip install -r requirements-paquet.txt` réussit sur `win_arm64` pour ces sept
paquets — certains n'ont peut-être aucune wheel publiée et forceraient une compilation
(donc un besoin de toolchain C/Rust sur le poste de build, que le paquet embeddable ne
prévoit pas) ou échoueraient purement. C'est exactement le travail de la tâche suivante :
lancer `tools/faire_paquet.py` pour de vrai sur le poste de sandr (ou une machine ARM64
empruntée), constater ce qui casse, et documenter le résultat ici — pas dans une nouvelle
section, en complétant celle-ci.

### Ce qui reste hors de cette vérification, à ne pas confondre avec un point réglé

- Le catalogue de modèles affiché côté proche n'a pas été filtré pour retirer les entrées
  `gemini:…` d'un paquet où le fournisseur est structurellement indisponible — l'écran
  Réglages du destinataire (étape D, toujours à faire) reste le bon endroit pour ça.
- `.env.example` continue de documenter `GEMINI_API_KEY` (il documente toutes les clés
  cloud possibles, y compris celles qu'un paquet donné ne peut pas honorer) — cohérent
  avec la décision existante de le laisser partir tel quel (§1, étape B).

### Reporté : le build réel sur ARM64 (décision d'Ilyann, 2026-08-10)

Tout ce qui précède a été vérifié sur x64 uniquement — aucune machine ARM64 n'était
accessible pendant cette session. Ilyann le fera lui-même, quand il aura accès au poste
de sandr ou à une machine ARM64 empruntée (§5). L'outillage est prêt côté script, un seul
piège à éviter :

```powershell
# URL_EMBEDDABLE (tools/faire_paquet.py) est câblée en dur sur "embed-amd64" — sans
# --embeddable, le script téléchargerait un runtime x64 et l'exécuterait (mal) sous
# émulation. Télécharger la release ARM64 à la main, puis :
python tools\faire_paquet.py --destinataire sandr --modules <à choisir> `
  --embeddable chemin\vers\python-3.12.10-embed-arm64.zip
```

Pas de contraintes par défaut la première fois (`--sans-contraintes`) : `tools/contraintes-paquet.txt` fige un arbre résolu sur x64, qui n'a aucune raison de correspondre aux
wheels disponibles pour `win_arm64` — l'imposer masquerait un échec de résolution
derrière un message `pip` moins clair. Une fois un premier build ARM64 réussi, en
générer un fichier de contraintes séparé si la reproductibilité devient nécessaire pour
cette architecture aussi (le fichier actuel ne prétend documenter que x64).

Ce qu'il faudra constater et noter ici, dans cette section, une fois cet accès obtenu :
si chacun des sept paquets du tableau ci-dessus s'installe (wheel existante) ou échoue
(compilation requise ou absente), et si les tests fonctionnels (import chromadb,
`PersistentClient`, les imports du tableau §0.2) passent une fois le paquet assemblé —
la même méthode que §0.5, sur la bonne architecture cette fois.
