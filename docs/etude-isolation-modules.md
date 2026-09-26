# Étude — isolation des modules générés par l'Atelier

> Document de décision, 2026-09-26. **Aucun code n'a été modifié.**
> Légende : **[V]** vérifié dans le code ou mesuré sur ce poste ;
> **[E]** estimé ; **[?]** question ouverte, avec le test qui la trancherait.
> Chiffrage : une « session » = une session de travail Claude Code, de la
> lecture aux tests verts.

---

## 0. En une minute

- Aujourd'hui, un module généré, **une fois approuvé**, a exactement les droits
  d'Épure : ceux de ton compte Windows. Côté navigateur, son composant a les
  droits de l'interface entière. **[V]**
- Le point le plus sous-estimé est le **frontend** : un `Component.tsx` généré
  lit le token, peut appeler `/code/execute` ou `/workshop/<id>/approve?force=true`,
  et donc exécuter n'importe quoi côté backend. Tant que ce côté n'est pas
  isolé, isoler le backend ne borne **pas** un module malveillant. Ça borne
  un module **maladroit**. **[V]**
- Un second trou concret : le **smoke test exécute le `router.py` généré
  AVANT que tu l'aies lu**, dans un sous-processus qui n'a aucune clé dans son
  environnement mais qui a accès au disque et au réseau. **[V]**
- Le worker (`module_worker.py`) est fait à 30 % à peu près : sa brique de
  lancement et ses tests passent, rien n'est câblé. Le seul module d'origine
  `workshop` installé (`slides`) **ne démarrerait pas** dans ce worker tel
  quel. **[V]**
- **Recommandation** : pendant la prépa, l'option d) renforcée + une CSP + un
  Job Object (≈ 3 à 4 sessions). On garde le worker, l'AppContainer et l'iframe
  pour le jour où un module généré sera partagé ou produit sans relecture.

---

## 1. État des lieux

### 1.1 Backend — ce qu'un module généré approuvé peut faire

`register_routers` fait `importlib.import_module(f"modules.{mid}.router")` dans
le process principal, pour tous les modules actifs, sans distinguer leur origine
(`core/module_registry.py`, `register_routers`). **[V]**

| Capacité | Aujourd'hui | Source |
|---|---|---|
| `os.environ` (clés API chargées par `load_dotenv`) | **oui**, en mémoire du process | même process |
| Token d'API | **oui** : `core.instance.instance_config.auth_token()`, et en clair dans `memory/instance_config.json` (`auth.token`) | `core/instance.py:333` |
| Objet `app`, moteurs, config | **oui** (`import main`, `core.runtime`) | même process |
| Système de fichiers | **tout ce que ton compte peut lire et écrire** (`open`, `pathlib`, `shutil.rmtree` ne sont pas dans la denylist) | `core/module_validate.py:37-46` |
| Réseau sortant | la denylist refuse `urllib/http/requests/httpx/aiohttp` à l'import. Contournable par construction (§5 de CLAUDE.md) | idem |
| Sous-processus | `subprocess`, `os.system`… refusés par l'AST. Contournable (alias via un autre module importé, `getattr` dynamique non littéral…) | idem |
| Imports arbitraires | **oui**, sauf la liste ci-dessus ; `core.*` n'est pas filtré | idem |

`slides` (seul module `origin: workshop` installé) importe d'ailleurs
`core.instance`, `core.jsonstore` et `core.paths` : la denylist ne l'interdit
pas, et la convention ne le prévoit pas non plus. **[V]**

**Le code généré tourne avant approbation, à deux endroits** **[V]** :

1. **Smoke test** (`smoke_test_staging`). Il est lancé automatiquement en
   tâche de fond après chaque génération dont le gate AST passe
   (`main.py`, `_background_smoke`), puis rejoué jusqu'à 2 fois par la boucle
   de réparation. Il tourne sous `sys.executable`, `cwd=backend/`, avec
   l'environnement `_make_exec_env()` : **aucune clé API ni aucun token dans
   l'environnement**, mais `PYTHONPATH` complet du backend, `USERPROFILE` et
   `APPDATA`, et un accès complet au disque et au réseau. En clair : il peut
   lire `backend/.env` et `instance_config.json` sur disque, puis les envoyer
   ailleurs. Timeout 90 s, pas de limite mémoire.
2. **Les moteurs de génération** (aider, claude) tournent avec les droits
   utilisateur. Leur confinement est celui de l'outil (`--add-dir`,
   `--allowedTools Read,Edit,Write`, cwd=staging), pas celui d'Épure. Le trou
   de jonction entre `_read_is_safe` et l'ouverture par aider est déjà noté
   dans `feuille-de-route.md` §5. Hors périmètre de cette étude : l'outil
   n'exécute pas le module.

### 1.2 Frontend — ce qu'un composant TSX généré peut faire

Les composants générés sont découverts par `import.meta.glob('./generated/**/*.tsx')`
et rendus **dans le même arbre React, sur la même origine** que le reste de
l'interface (`src/modules/registry.ts:83`). **[V]**

| Capacité | Aujourd'hui |
|---|---|
| Lire le token | **oui** : `localStorage['epure.apiToken']`, `getToken()` exporté par `api.ts` ; et `GET /pair` le rend à toute requête locale (`main.py:1120`) |
| Appeler tous les endpoints | **oui**. Parmi eux : `/code/file` + `/code/execute` (exécution Python arbitraire si le module `code` est installé, ce qui est le cas ici), `/workshop/<id>/approve?force=true` (installer du code backend sans validation), `/ws/workshop` (piloter l'Atelier), `PUT /settings/api-keys` (réécrire `.env`), `/admin/execute` (actions sur fichiers de `admin_engine`) |
| Lire les clés API | non directement : `GET /settings/api-keys` ne renvoie que des booléens **[V]** ; mais la voie `/code/execute` y mène |
| `fetch` vers l'extérieur | **oui**. La validation TSX refuse seulement `dangerouslySetInnerHTML`, `eval(` et `new Function(` (`module_validate.py:291-340`) |
| CSP | **aucune** : ni `<meta>` dans `index.html`, ni en-tête posé par FastAPI, ni configuration Vite **[V]** |

L'app s'ouvre dans le **navigateur par défaut** (`epure_tray.py` :
`webbrowser.open`), pas dans une webview dédiée. **[V]** Pour une future CSP :
`@monaco-editor/react` (module `code`) charge Monaco depuis `cdn.jsdelivr.net`
par défaut, et aucun `loader.config` n'est posé dans `src/`. **[V]**

### 1.3 `module_worker.py` — ce qui existe, ce qui manque

**Existe et passe** (5/5 tests en 16,8 s sur ce poste, 2026-09-26 ; le test est
découvert automatiquement par `unittest discover`, donc il tourne en CI). **[V]**
Note : CLAUDE.md §7 dit « untracked à ce jour ». C'est périmé, les deux
fichiers sont suivis depuis `64c19f1`.

- `build_worker_env` : environnement réduit à une allowlist (PATH, SYSTEMROOT,
  TEMP…), sans `USERPROFILE`, `APPDATA`, `PYTHONPATH` ni clé.
- Faux paquet `core` avec seulement `core.runtime` (shim `llm` qui relaie en
  HTTP vers `/capabilities/llm/*`, `SSE_HEADERS`) ; un `MetaPathFinder` refuse
  tout autre `core.*` dès l'import.
- App FastAPI minimale sur `127.0.0.1:<port>`, en-tête `X-Epure-Worker-Key`
  exigé (`hmac.compare_digest`).
- `spawn_worker`, `wait_healthy`, `find_free_port`.

**Mesures sur ce poste (module `hello`, venv dédié)** **[V]** : prêt en
**1,6 à 2,7 s** ; **≈ 51 Mo** pour le python réel, plus 4 Mo pour le lanceur du
venv et 7 Mo pour un `conhost.exe`. Le lanceur `.venv\Scripts\python.exe`
relance le vrai interpréteur en processus **enfant**. `terminate()` sur le
lanceur n'a laissé aucun orphelin (1 essai).

**Manque** **[V]** :

- les routes `/capabilities/*` du principal (aucune n'existe) ;
- le proxy `/<id>/*` principal → worker, SSE compris ;
- l'hôte des workers (démarrage paresseux, redémarrage après crash, arrêt au
  shutdown) : `spawn_worker` n'est appelé que par les tests ;
- dans `register_routers`, la branche qui n'importe pas un module `workshop`
  mais monte son proxy ;
- **un stockage et une résolution de modèle dans le shim** : `slides` importe
  `core.instance`, `core.jsonstore` et `core.paths`, que le garde refuse. Il
  meurt au démarrage du worker ;
- la correction déjà notée : `_harden_sys_path` compare `backend/` en chaîne,
  pas résolu (`feuille-de-route.md` §5) ;
- détail : le code utilise **une seule clé** dans les deux sens (proxy→worker
  et worker→capabilities), alors que le design en prévoit deux. Ce n'est pas
  un secret du principal, mais le principal devra déduire l'id du module de
  cette clé, jamais de la requête.

**Pourquoi ce n'est pas branché** : il n'y a pas de plan de câblage
(`feuille-de-route.md` §5 le demande), le chantier touche le montage de tous
les modules, et le périmètre de `docs/isolation_modules.md` est **périmé**. Il
cite dix modules `workshop` (astral, pong, snake…), qui sont tous dans
`_backups/`. Il n'en reste qu'un, `slides`. **[V]**

### 1.4 Ce dont un module légitime a besoin (fixe la surface d'API)

| Module | Origine | Services de core appelés | Isolable ? |
|---|---|---|---|
| `hello` | builtin | aucun | trivial |
| `slides` | **workshop** | `llm.generate`, `SSE_HEADERS`, `jsonstore` (un fichier `slides_decks.json`), `resolve_data_dir`, `modele_local_defaut`, `est_modele_cloud` ; `python-pptx` | **oui**, avec stockage JSON + résolution de modèle dans les capabilities |
| `rangement` | catalogue | `llm`, `SSE_HEADERS`, `core.models` (listes FLM/Ollama) | oui avec une capability « modèles » |
| `flashcards`, `reviseur` | catalogue | `llm`, `flashcards_engine`, `memory`, `rag`, `modele_pour_tache` | coûteux (RAG + mémoire) |
| `kholle` | catalogue | `rag`, `llm`, WebSocket, `ws_require_token` | coûteux (WS) |
| `docs` | catalogue | `docanalysis`, `memory`, uploads, WebSocket | coûteux |
| `code` | catalogue | `codeagent` (exécution de code), WebSocket | sans objet : il exécute du code par nature |
| `encre`, `image` | builtin | `hmer`, `rag` / `llm`, `httpx` vers ComfyUI local | sans objet (builtin) |

**[V]** pour les imports (relevé sur les `router.py` de `backend/modules/` et de
`modules-catalogue/`).

**Conséquence** : la surface minimale qui couvre le seul module généré réel
comprend `llm.generate` et `llm.stream` (modèle résolu côté principal), un
document JSON par module avec la sémantique `transaction`, et une lecture
« modèle local par défaut / est-ce du cloud ». C'est ce que prévoit
`isolation_modules.md`, plus la résolution de modèle. Les modules du catalogue
ont besoin de RAG, de mémoire et de WebSocket. Les isoler reviendrait à
exposer presque tout `core`, donc à ne plus rien isoler. **Ils doivent rester
en process et être considérés de confiance** : ils vivent dans le dépôt et
sont relus comme du code du cœur.

Lien avec §3.7 : les capabilities sont l'endroit naturel où **forcer
« local par défaut »** pour les modules générés (un `model=None` résolu par
`modele_pour_tache`, pas par `modèle_actif`). **[E]**

---

## 2. Modèle de menace (une demi-page)

**Actif à protéger** : les clés API (argent prépayé, §3.7), le token d'instance
(qui donne tout Épure), les données personnelles (fiches, historique,
`memory/`), l'intégrité du poste (fichiers utilisateur, démarrage automatique).

| # | Menace | Vecteur réaliste chez toi | Gravité |
|---|---|---|---|
| M1 | **Code halluciné** qui détruit ou écrase (`rmtree` d'un chemin mal calculé, écriture dans `memory/`) | le plus probable : un modèle local 7B qui « range » | haute, probabilité moyenne |
| M2 | **Fuite accidentelle** d'un secret (log de `os.environ`, secret renvoyé dans une réponse) | modèle qui « débogue » | moyenne |
| M3 | **Code malveillant par injection de prompt** : un document lu en `--read` (`grant_read`), du texte collé dans la spec, une page copiée | faible aujourd'hui : l'Atelier ne lit ni web ni RAG de lui-même, seulement ce que tu lui donnes | haute, probabilité faible |
| M4 | **Exfiltration** clés/token vers une URL | conséquence de M3, ou M2 via le réseau | haute |
| M5 | **Persistance** : écrire un autre module, un script dans Démarrage, une clé `HKCU\…\Run`, modifier un module approuvé sur disque | conséquence de M3 | haute |
| M6 | **DoS local** : boucle infinie, fuite mémoire, fork | M1 | basse (tu relances) |

**Hors périmètre, explicitement** : un attaquant qui a déjà le token, ou une
session sur ton compte Windows. Un tel attaquant n'a pas besoin de l'Atelier.
Hors périmètre aussi : la chaîne d'approvisionnement pip/npm, et le moteur de
génération lui-même (aider/claude sont des outils de confiance, bornés par
leurs propres garde-fous).

**Remarque qui oriente tout le reste** : M1, M2 et M6 sont des **accidents**.
Une relecture sérieuse et des garde-fous simples les réduisent beaucoup. M3,
M4 et M5 demandent une **frontière**, et une frontière ne vaut que si elle
couvre à la fois le backend **et** le frontend (§1.2).

---

## 3. Options

Chiffrage **[E]** sauf mention contraire. « Lignes » = lignes ajoutées ou
modifiées, tests compris.

### a) Worker sous-processus (câbler `module_worker`)

Modules `origin: workshop` seulement : proxy `/<id>/*`, `/capabilities/{llm,storage,models}`,
hôte des workers, environnement vidé, sans token.

- **Protège** : la mémoire du principal (clés dans `os.environ`, token en
  mémoire, `app`, moteurs) ; un crash, un `sys.exit` ou une boucle au chargement
  ne touche plus l'app ; point unique pour les quotas et le « local par défaut ».
- **Ne protège pas** : la **lecture du disque** (`backend/.env`,
  `memory/instance_config.json` qui contient le token), le réseau sortant,
  l'écriture n'importe où sous ton profil, la persistance. Contre un module
  malveillant (M3 à M5), le gain est **faible** : `open(".env")` suffit. Rien
  non plus contre le frontend.
- **Coût** : 1 session de plan (`docs/isolation-cablage.md`, déjà demandé),
  puis 4 à 6 sessions ; ≈ 1 000 à 1 300 lignes (hôte ≈ 250, proxy SSE ≈ 150,
  capabilities ≈ 200, registre ≈ 50, shim storage/modèles ≈ 80, tests ≈ 450,
  docs/CONVENTIONS ≈ 100).
- **Modules existants** : `slides` à migrer vers le shim (≈ 30 lignes :
  `jsonstore` → `runtime.storage`, `modele_local_defaut` → capability).
  Catalogue et builtin inchangés.
- **Paquet distribué** : l'Atelier y est désactivé (`EPURE_ATELIER=0`), donc
  pas de module généré sauf s'il est embarqué. `module_worker` s'appuie sur
  `sys.executable`. Avec le Python embarqué 3.12, le `._pth` fixe `sys.path`,
  `PYTHONPATH` est ignoré, et le worker n'en dépend pas. **[?]** À vérifier par
  un `test_paquet` qui lance un worker depuis le zip. Smart App Control :
  aucun nouveau binaire natif.
- **Latence / mémoire** : démarrage à froid 1,6 à 2,7 s **[V]** (`hello`).
  `slides` importe `pptx`, compter +0,3 à 1 s **[E]**. ≈ 60 Mo par worker actif
  **[V]**. Un saut loopback par requête : quelques ms **[E]**.
- **Redémarrage** : compatible. Le proxy est monté au démarrage comme un routeur
  (`ecart_redemarrage` inchangé, `_signature` reste valable). **Possibilité
  nouvelle** : relancer un worker sans redémarrer l'app, puisque aucune route
  n'est démontée. C'est un changement de l'IMPÉRATIF §3.3 : à valider par toi,
  pas à supposer.

### b) Durcissement OS Windows, en plus de a)

Trois mécanismes, très différents. Tous sont accessibles **sans droits admin**
par `ctypes` (pas de `pywin32` : ce serait une DLL native de plus pour Smart
App Control).

| Mécanisme | Bloque | Ne bloque pas | Coût | Risques |
|---|---|---|---|---|
| **b1. Job Object** (`CreateJobObjectW`, `KILL_ON_JOB_CLOSE`, plafond mémoire, `ACTIVE_PROCESS_LIMIT`) | M6 (mémoire, CPU), les sous-processus (plafond de processus actifs → `subprocess` échoue même si l'AST est contourné), les orphelins | lecture et écriture disque, réseau | 1 à 2 sessions, ≈ 150 lignes | **[?]** avec le lanceur du venv, le python réel est un **enfant** lancé tout de suite : entre-t-il dans le Job si on assigne le lanceur après `Popen` ? Test : assigner le PID du lanceur, puis lire `JobObjectBasicProcessIdList` ; si l'enfant manque, lancer `sys._base_executable` avec le `site-packages` du venv, ou créer le processus suspendu. |
| **b2. Jeton à intégrité basse** (`DuplicateTokenEx` + `SetTokenInformation(TokenIntegrityLevel)` + `CreateProcessAsUserW`) | **l'écriture** hors `%LOCALAPPDATA%Low` → M1 (destruction), M5 (Démarrage, `HKCU\Run`, modification d'un module approuvé) | **la lecture** : par défaut, Windows n'interdit que l'écriture vers le haut, pas la lecture. `.env` et le token restent lisibles. Réseau ouvert. | 2 sessions, ≈ 250 lignes (`subprocess.Popen` ne prend pas de jeton, il faut refaire le lancement et la tuyauterie des handles) | `TEMP` à rediriger vers LocalLow, `__pycache__` non écrit (imports un peu plus lents). **[?]** Un processus basse intégrité peut-il écouter sur 127.0.0.1 et être joint par le principal ? Test : lancer le worker `hello` à intégrité basse et appeler `wait_healthy`. |
| **b3. AppContainer** (`CreateAppContainerProfile` + `SECURITY_CAPABILITIES` via `UpdateProcThreadAttribute`) | **lecture ET écriture** hors des dossiers explicitement ouverts au conteneur, réseau sortant (sans la capability `internetClient`) → la seule vraie frontière de M3 à M5 côté backend | rien de notable côté backend, s'il est bien fait | 5 à 8 sessions, ≈ 500 lignes, incertitude **forte** | Les ACL de `.venv/` et du Python de base, ou du Python embarqué du paquet, doivent s'ouvrir au conteneur (`icacls` sur des dossiers qui t'appartiennent : pas d'admin). **[E]** L'AppContainer bloque le **loopback** par défaut, et l'exemption demande l'admin. Il faudrait alors changer le transport TCP pour des pipes nommés ou hérités, et récrire le proxy. **[?]** Test : worker `hello` en AppContainer, puis connexion loopback depuis le principal. **[?]** Comportement de Smart App Control pour un python.exe lancé en AppContainer : à mesurer sur ton poste. |

Écarté **[V]** : Windows Sandbox et Hyper-V n'existent pas sous Windows 11
**Famille** (ton poste : Windows 11 Home 10.0.26200). Un utilisateur Windows
dédié demande l'admin à la création et un mot de passe stocké : non retenu
**[E]**.

- **Redémarrage** : compatible pour les trois, puisque seul le lancement du
  worker change.
- **Paquet** : b1 et b2 sans impact. b3 impose de poser des ACL sur le dossier
  décompressé au premier lancement **[E]**.

### c) Frontend : origine séparée, iframe, CSP, token à portée limitée

| Sous-option | Protège | Ne protège pas | Coût |
|---|---|---|---|
| **c1. CSP seule** (en-tête posé par FastAPI en mode paquet, `<meta>` en dev) : `connect-src 'self' <backend> ws:<backend> https://cdn.jsdelivr.net`, `img-src 'self' data: blob:`, `frame-ancestors 'none'`, `object-src 'none'` | l'exfiltration par `fetch`, XHR, WebSocket ou image vers une URL tierce (M4 côté navigateur) ; bonus : réduit l'impact d'un XSS dans le rendu du chat | le vol du token **suivi de son usage local** (`/code/execute`…), l'exfiltration par navigation (`location = 'https://…?' + token` : CSP ne sait pas l'interdire) | 0,5 à 1 session, ≈ 60 lignes |
| **c2. iframe `sandbox="allow-scripts"`** (sans `allow-same-origin` : origine opaque, pas de `localStorage` partagé, pas de token) + CSP `connect-src 'none'` dans l'iframe + pont `postMessage` que le parent limite à `/<id>/*` | M3 à M5 **côté navigateur** : le composant n'atteint ni le token, ni `/code/*`, ni `/workshop/*` | le backend du module (c'est a) et b)) | 4 à 7 sessions, ≈ 800 lignes : une entrée Vite « hôte de module », un `api.ts` de remplacement dans l'iframe (qui garde la même API, donc les composants ne changent pas), le relais de `SharedModuleProps` (`playSpeech`, `onNavigate`…), Tailwind et l'UI partagée recompilés dans l'iframe, redimensionnement, tests |
| **c3. Token à portée limitée par module** | seulement avec c2 : sans iframe, le composant lit le token principal dans `localStorage` et la portée ne sert à rien | — | +1 session, ≈ 150 lignes (émission, vérification dans le middleware) |
| **c4. Cookie HttpOnly** à la place de `localStorage` | le vol du token par lecture JS | l'**usage** du token : le cookie part avec chaque `fetch` même origine, donc un composant hostile appelle quand même `/code/execute`. Il faudrait en plus un jeton anti-CSRF. Et le WebSocket `?token=` casse | 1 à 2 sessions ; faible valeur sans c2 |

- **Modules existants** : c1 peut casser Monaco (CDN, à autoriser) et toute
  image distante affichée dans le chat. **[?]** Test : une CSP en mode
  `Content-Security-Policy-Report-Only` pendant une semaine d'usage normal,
  puis lire la console. c2 ne concerne que les composants `workshop` (`slides`
  aujourd'hui) ; le cœur et le catalogue restent dans l'arbre principal.
- **Paquet** : c1 est un en-tête dans `main.py`. c2 ajoute une entrée au build,
  et l'élimination du code de l'Atelier (`VITE_ATELIER=0`, `test_paquet.py`)
  doit rester vraie.
- **Latence** : c2, un aller-retour `postMessage` par appel (< 1 ms) **[E]** ;
  une iframe par module ouvert (≈ 10 à 30 Mo côté navigateur) **[E]**.
- **Redémarrage** : sans rapport (le frontend est construit ou servi par Vite).

### d) Politique : relecture humaine explicite, catalogue de confiance

Une bonne partie **existe déjà** **[V]** : les trois fichiers sont affichés
avant approbation, avec un diff en édition (`read_staging`) ;
`approve(force=True)` demande un `window.confirm` ; approuver refait la
validation ; rien n'est chargé avant le redémarrage.

Ce qui **manque** pour que « relu avant exécution » soit vrai :

1. **Le smoke test tourne avant la relecture** (§1.1). Deux façons de
   corriger : ne le lancer qu'après un clic « j'ai lu, tester », ou le lancer
   dans l'environnement le plus dur disponible (b1 au minimum, b2 si le test
   le permet). La boucle de réparation automatique pose la même question : le
   code réparé est exécuté, pas relu.
2. **L'approbation n'est pas liée au contenu.** `register_routers` charge
   n'importe quel `router.py` présent sur disque. Si un fichier est modifié
   après approbation (par un autre outil, un agent, une passe mal confinée), il
   tourne au redémarrage suivant. Il faudrait **épingler l'empreinte approuvée**
   (manifest + router + composant, en réutilisant `_signature`) et refuser au
   démarrage un module `workshop` dont l'empreinte a changé. Il faut la
   stocker via `jsonstore`, **distincte de l'état actif**, pour ne pas recréer
   le problème `modules_state.json` (§3.3).
3. **Relecture guidée** : afficher en tête de revue ce que le module *importe*
   (`core.*`, `pathlib`, `shutil`, `open`) et *quels endpoints il appelle* côté
   TSX (extraction statique des `apiFetch`). Un `router.py` de 700 lignes ne se
   relit pas ligne à ligne. Une liste de 8 lignes, si. **[E]**
4. `force=True` : le garder, mais le journaliser dans l'empreinte (« approuvé
   malgré erreurs »).

- **Protège** : M1 et M2 surtout (tu vois le code avant qu'il tourne), M5 en
  partie (point 2 : plus de modification silencieuse d'un module approuvé).
- **Ne protège pas** : ce que tu ne vois pas en relisant (code volontairement
  obscur, M3), et tout ce qu'un module approuvé fait ensuite.
- **Coût** : 1,5 à 2,5 sessions, ≈ 300 lignes. Aucun impact sur les modules
  existants, sauf une réapprobation unique de `slides` pour poser son
  empreinte. Paquet : aucun (Atelier désactivé). Latence : nulle.
  **Redémarrage** : c'est son terrain naturel (vérification au démarrage).

### e) (option ajoutée) Modules générés **sans Python**

Le LLM ne génère plus de `router.py` par défaut. Le module est un composant
qui appelle des endpoints **génériques du cœur** : `/modules/<id>/storage`
(un JSON par module, jsonstore) et `/modules/<id>/llm` (local par défaut,
§3.7). La génération d'un backend Python devient une option explicite,
réservée au jour où a) existe.

- **Protège** : tout le risque backend (M1, M2, M5, M6 côté Python) **pour
  cette classe de modules**, sans worker. D'après les modules supprimés
  (`_backups/` : pong, snake, clicker, minuteur…), c'est la majorité de ce que
  l'Atelier produit en pratique **[E]**.
- **Ne protège pas** : le frontend (le composant a toujours les droits de
  l'interface) ; les modules qui ont vraiment besoin de Python (`slides`,
  pour produire un `.pptx`).
- **Coût** : 2 à 3 sessions, ≈ 400 lignes (endpoints, mode du prompt et de
  `CONVENTIONS.md`, validation « pas de router », smoke test inutile dans ce
  mode). Les endpoints génériques sont exactement les futures capabilities
  de a) : ce travail n'est **pas perdu** le jour où l'on câble le worker.
- Paquet : aucun impact. Redémarrage : un module sans router n'a rien à
  monter, donc son activation n'exige même plus de redémarrage (le composant
  est chargé par le frontend). À confirmer avec `ecart_redemarrage`, qui ne
  regarde déjà que les modules avec `router.py` **[V]**.

### Synthèse

| | M1 halluciné | M2 fuite accid. | M3-M5 malveillant | M6 DoS | Sessions | Risque de faisabilité |
|---|---|---|---|---|---|---|
| a) worker | ~ (crash isolé, disque ouvert) | **oui** (env) | **non** (disque + frontend) | ~ | 5-7 | faible |
| b1) Job | non | non | non | **oui** | 1-2 | moyen (lanceur venv) |
| b2) intégrité basse | **oui** (écriture) | non | partiel (persistance) | non | 2 | moyen |
| b3) AppContainer | **oui** | **oui** | **oui côté backend** | non | 5-8 | **fort** (loopback, ACL) |
| c1) CSP | non | ~ | partiel (fetch) | non | 0,5-1 | faible |
| c2) iframe (+c3) | non | non | **oui côté navigateur** | non | 5-8 | moyen |
| d) relecture renforcée | **oui** | **oui** | ~ (dépend de ta lecture) | non | 1,5-2,5 | faible |
| e) sans Python | **oui** (classe) | **oui** (classe) | non (frontend) | **oui** (classe) | 2-3 | faible |

Une vraie frontière contre un module malveillant demande **a + b3 + c2**,
soit ≈ 15 à 23 sessions avec deux inconnues fortes. Aucune option seule n'y
arrive.

---

## 4. Recommandation

### Étapes, dans l'ordre. Chacune se livre seule et réduit un risque réel

| # | Étape | Risque réduit | Sessions | Quand |
|---|---|---|---|---|
| 1 | **d.1 : plus d'exécution avant lecture.** Smoke test et boucle de réparation après un « j'ai lu » explicite | M1-M4 sur du code **jamais vu** : aujourd'hui, le pire trou à coût nul | 0,5-1 | prépa |
| 2 | **c1 : CSP** en mode *Report-Only* une semaine, puis appliquée | exfiltration navigateur (M4), XSS du chat | 0,5-1 | prépa |
| 3 | **d.2 + d.3 : empreinte approuvée** vérifiée au démarrage, **résumé des imports et endpoints** en tête de revue | M5 (modification silencieuse), qualité de la relecture | 1-1,5 | prépa |
| 4 | **b1 : Job Object** sur le smoke test (et sur `codeagent.execute_code`, même bénéfice) : plafond mémoire, pas de processus enfant, kill-on-close | M6, contournement `subprocess` de l'AST | 1-2 | prépa si le temps le permet |
| 5 | **e : modules générés sans Python par défaut**. Les endpoints génériques préfigurent les capabilities | tout le risque backend pour la majorité des modules | 2-3 | après la prépa, ou si l'Atelier sert plus |
| 6 | **a : câbler le worker**, d'abord le plan `isolation-cablage.md`, puis `slides` migré | mémoire du principal, crash, point de contrôle du « local par défaut » | 5-7 | quand un 2ᵉ module Python généré existe |
| 7 | **b2 puis b3, et c2** | la vraie frontière | 12-18 | seulement si des modules générés sont **partagés** ou produits sans relecture |

**Pendant l'année de prépa, raisonnablement** : les étapes 1 à 3 (≈ 2 à 3,5
sessions, faible risque, aucune inconnue), plus 4 si une session se libère.
Tout le reste peut attendre sans que le risque augmente, **à une condition** :
le périmètre ne change pas (tu es seul, tu relis, rien n'est partagé).

Une étape de ménage documentaire, qui ne compte pas comme une session : mettre
à jour CLAUDE.md §7 (fichiers suivis, test en CI) et le périmètre de
`isolation_modules.md` (dix modules → `slides`). CLAUDE.md est normatif, donc
à faire avec ton accord, pas dans cette PR.

### L'option d) seule suffit-elle pour ton usage actuel ?

**Oui, à condition qu'elle soit complétée des points d.1 et d.2**, c'est-à-dire
les étapes 1 et 3. Dans son état actuel, **non**, pour une raison précise : le
smoke test exécute le code avant que tu l'aies vu, ce qui annule le principe
« relu avant exécution ».

Pourquoi ça suffit une fois corrigé :

- Les menaces probables chez toi sont M1 et M2, des accidents. La relecture les
  attrape, et les garde-fous existants (AST, validation à l'approbation,
  chargement au redémarrage seulement) coupent les plus bêtes.
- M3 demande qu'un contenu hostile atteigne le prompt de l'Atelier. Aujourd'hui,
  seules ta spec et les fichiers que tu accordes y entrent : pas de web, pas de
  RAG automatique.
- Le coût de la frontière réelle (15 à 23 sessions, deux inconnues Windows)
  est hors de proportion avec un Atelier « moyennement utile » et un seul
  module généré en service.
- Ce qui est payé maintenant (CSP, empreinte, Job Object) reste utile quand on
  ira plus loin. Rien n'est jeté.

**Ce qui rendrait d) insuffisante**, et c'est le signal pour passer aux étapes 5
à 7 : installer un module généré par quelqu'un d'autre ; activer l'Atelier dans
le paquet distribué ; laisser l'Atelier lire du web ou du RAG de lui-même ;
approuver sans lire (réparation automatique en boucle, lots de modules).

---

## 5. Questions ouvertes, et le test qui tranche chacune

1. **Job Object et lanceur du venv** : l'enfant python entre-t-il dans le Job
   si on assigne le lanceur après `Popen` ? → assigner, puis lire
   `JobObjectBasicProcessIdList`.
2. **Intégrité basse et loopback** : un worker à intégrité basse
   peut-il écouter sur 127.0.0.1 et répondre au principal ? → `hello` à
   intégrité basse + `wait_healthy`.
3. **AppContainer et loopback / ACL** : faut-il abandonner TCP pour des pipes ?
   Quelles ACL minimales sur `.venv` et le Python de base ? → `hello` en
   AppContainer, deux essais : un avec connexion TCP, un avec pipe hérité.
4. **Smart App Control** avec un processus AppContainer ou à jeton modifié →
   même essai, sur ton poste où SAC est actif.
5. **CSP** : qu'est-ce qui casse en usage réel (Monaco, images distantes du
   chat, KaTeX) ? → une semaine en `Report-Only`.
6. **Worker dans le paquet** (Python embarqué, `._pth`) → un `test_paquet`
   qui démarre `hello` en worker depuis le zip.
7. **Coût réel de `slides` en worker** (import de `pptx`, mémoire) → la même
   mesure que §1.3, sur `slides` une fois migré.
