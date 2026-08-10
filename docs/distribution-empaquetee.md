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
| B — script de constitution du paquet | **faite** — `tools/faire_paquet.py`, hors de ce qui est livré ; `backend/test_paquet.py` (27 tests) tient les règles d'exclusion |
| C — installeur minimal | à faire |
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

### Étape C — Installeur minimal pour le proche

Dézippe l'archive, installe Ollama s'il est absent, tire le modèle, pose un raccourci
Bureau. Plus de détection Python/Node/git.

Vérification : sur une machine où rien n'est installé, mesurer le temps et le nombre
d'étapes réellement franchies sans intervention manuelle.

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

Ce qui n'est pas mesuré : si le blocage se reproduit sur une machine **jamais** exposée à
ces fichiers (ici, `pip` venait de les écrire, et le poste avait déjà vu ce genre de DLL).
À vérifier sur la machine du premier destinataire.

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
au-delà de sandr. À construire à la demande, sur une machine ARM empruntée.

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
- **`win_arm64`** — tout le §0 a été mesuré sur x64. `chroma-hnswlib` a disparu de
  l'arbre, mais `grpcio` reste, et la question d'une machine ARM est intacte.
- **Le premier lancement du RAG chez le proche** — l'installation à la demande de torch
  (16 paquets, dont `torch 2.13.0` et `transformers 5.15.0`) n'a jamais été exécutée
  depuis un Python embeddable, seulement résolue en `--dry-run`.
- Le temps qu'Ilyann devra consacrer à reconstituer un paquet par destinataire — workflow
  manuel acceptable pour deux ou trois proches, à revoir si ça grandit.
