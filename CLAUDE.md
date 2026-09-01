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
# Relance de DEV après un git pull : node résiduels, pull, npm ci (avec
# réparation EPERM), build, libération du port 8000, uvicorn au premier plan.
# Un raccourci de bureau y mène (-PoserRaccourci pour le (re)créer).
.	ools\dev-epure.ps1
.	ools\dev-epure.ps1 -Diagnostic   # tout sauf uvicorn, pour vérifier l'état

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
python test_web_statique.py       # interface servie par FastAPI + EPURE_ATELIER=0
python test_logs_secrets.py       # le token ne sort pas dans les logs (§6)
python test_memory_sans_llm.py    # aucun appel LLM sur le chemin d'un message (§8)
python test_voice_indisponible.py # voix absente proprement (paquet, pas modèle) — ARM64
python test_paquet.py             # tools/faire_paquet.py — ce qui ne doit PAS sortir
python test_installeur.py         # installeur du paquet : mise à jour sans perte de données
python test_websocket_dependance.py  # uvicorn sans lib WebSocket → tout /ws/* mort (§8)
python test_models_cloud_sans_cle.py # un fournisseur sans clé ne rend aucun modèle
python test_embedding_install.py  # mise à disposition du modèle d'embedding (§3.4)
python test_wordpiece.py          # parité du tokeniseur Python pur (§3.4)
python test_dependances_declarees.py  # onnxruntime déclaré en DIRECT, jamais transitif (§8)
python test_encodage_scripts.py   # les .ps1 versionnés restent en ASCII pur (§8)
python test_dev_epure.py          # stderr non fatal dans tools/dev-epure.ps1 (§8)
python test_mise_a_jour.py        # l'archive s'applique sans s'imbriquer (§8)
python test_raisonnement_stream.py   # le raisonnement d'Ollama n'est plus jeté (§8)
python test_ingestion_documents.py   # formats lus par le RAG : pptx/xlsx/docx réels
python test_vision_images.py      # indexation d'une image : décrite par un modèle vision, pas un placeholder (§3.3 bis)
python test_taches_locales.py     # aucune tâche de fond ne part en cloud (§3.7)
python test_module_isolation.py   # worker isolé — CHANTIER, cf. §7
python integration_modules_mount.py  # LOURD : core.runtime + le vrai store vectoriel
python integration_vector_store.py   # LOURD : parité core/vector_store.py ↔ chromadb
```

`integration_vector_store.py` exige un `pip install chromadb`, qui n'est plus une
dépendance du projet : il compare le store actuel à celui qu'il a remplacé. C'est
sa raison d'être et non un oubli — il ne peut pas se passer des deux côtés de la
comparaison. Même chose pour `parite_vectorielle.py` et `migrer_vectoriel.py`,
qui lisent l'ancien index (§3.4).

**Un nouveau `backend/test_*.py` est pris en compte sans toucher au workflow** :
la CI tourne en `unittest discover` depuis le commit `7e3bf8c`. Ce n'est plus une
liste de `run:` nommés — cette liste avait laissé 4 fichiers sur 6 ne jamais
tourner. Nommer un fichier `integration_*.py` au lieu de `test_*.py` est ce qui
l'exclut de la découverte (cas de `integration_modules_mount.py`, qui charge
le vrai store vectoriel et tourne dans le job `integration`, manuel).

**Quatre lanceurs, quatre publics** — ne pas les confondre :
`tools/dev-epure.ps1` (ce poste, après un pull, logs visibles),
`epure_tray.py` (usage normal : icône, Ollama, Vite, console masquée),
`tools/Installer-Epure.cmd` + `installer-epure.ps1` (**le destinataire d'un
paquet**, à ne pas toucher pour un besoin de dev),
`tools/Mettre-A-Jour-Epure.cmd` + `mettre-a-jour-epure.ps1` (**le destinataire
qui a le DÉPÔT** et refait le cycle complet chez lui : code à jour, `npm.cmd
install`, `faire_paquet.py`, arrêt de l'instance, installation — cinq étapes, un
double-clic, arrêt net à la première qui échoue). Ce dernier existe parce que sur
la machine cible `git` est inutilisable — Smart App Control y bloque
`git-remote-https.exe` et `libcurl-4.dll` — donc la mise à jour du code passe par
l'archive `main.zip`, avec le piège d'imbrication que
`backend/test_mise_a_jour.py` verrouille. Le premier n'implémente PAS la
décision « ce port est-il à moi ? » : il appelle `lanceur.py`, qui la porte avec
ses 37 tests — deux implémentations divergeraient, et celle qui se tromperait
tuerait le processus de quelqu'un d'autre.

### Tests frontend — `npm test` depuis `frontend/`

**vitest + jsdom + @testing-library/react**, arrivés le 2026-08-23. Bloquants en
CI, `frontend/vitest.config.ts`, fichiers `src/**/*.test.tsx`.

Ils existent pour une classe de bug que ni `tsc -b` ni eslint ne peuvent voir :
**un `as` posé sur un `r.json()` est une affirmation, pas une vérification.**
Le compilateur croit l'annotation ; le serveur, lui, répond parfois un corps
d'erreur (`{"detail": …, "type": …}` du gestionnaire d'exceptions, un 401 avant
appairage, un 404 sur une instance qui n'a pas la route). Le champ annoncé est
alors `undefined`, le `.catch()` ne voit rien puisque `r.json()` a réussi, et la
faute n'apparaît qu'au rendu suivant — sur un `.length`, dans un chunk minifié
où la trace ne nomme même pas la ligne. C'est exactement ce qui s'est produit
dans le panneau fichiers du module Docs (§8).

**Écrire les nouveaux tests de composant en éprouvant la FORME des réponses**,
pas seulement le cas nominal : `ModuleBar.test.tsx` rejoue le corps de réponse
réel d'un backend qui refuse, et son idiome (`liste()`, `categories()`, `dico()`
dans `ModuleBar.tsx`) est ce qu'il faut reprendre à chaque frontière `.json()`.

```powershell
cd frontend
npm test              # vitest run
npx vitest             # mode watch, pendant le développement
```

### Écart de version Python — piège actif

Les `.pyc` locaux sont en `cpython-314` (Python 3.14) ; la CI tourne en **3.12**
(`ci.yml`). Du code qui marche en local peut casser en CI. Si tu utilises une
syntaxe récente, vérifie sa disponibilité en 3.12.

### Écart de DÉPENDANCES local/CI — le même piège, moins connu

Le job `backend` de la CI n'installe pas `requirements.txt` mais un **jeu
minimal** (l'en-tête de `ci.yml` le justifie ligne par ligne) : ni
**`faster-whisper`, ni `piper-tts`**. Sur le poste d'Ilyann tout est installé.
(`onnxruntime` y EST depuis le 2026-08-26 : il ne pèse plus 198 Mo de wheels mais
14 Mo, et il est embarqué dans le paquet — l'en garder dehors ferait tourner la CI
dans une configuration qui n'existe nulle part. Ce qui reste hors du job, c'est le
*modèle* : 90 Mo de poids que `_test_env` empêche de télécharger.) Un test qui touche un moteur vocal ou vectoriel peut
donc passer en local et échouer en CI **sans une ligne de syntaxe récente** — et
c'est arrivé : un garde-fou « refuser si `piper-tts` est absent » a fait tomber
sept tests de `test_models_dir.py`, qui neutralisaient `_load` mais pas la
présence du paquet. Cause suivante enchaînée : une assertion d'ÉGALITÉ sur la
liste des paquets manquants, vraie avec un seul absent, fausse avec deux.

Deux réflexes :

- une assertion sur ce qui est *installé* se formule en **inclusion**, pas en
  égalité, ou se garde par un `if _module_present(...)` ;
- avant de pousser un changement qui touche ces moteurs, rejouer la suite avec
  les paquets bloqués — un `sys.meta_path` qui lève `ImportError` sur
  `piper` / `faster_whisper` / `ctranslate2` / `onnxruntime` reproduit
  la condition en une vingtaine de lignes, et c'est ce qui a attrapé le second
  échec avant la CI plutôt qu'après.

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
  attribut. Raison historique : `RAGEngine` importait torch +
  sentence-transformers (~30 s à chaud, 2 min à froid) et bloquait uvicorn au
  point que `/health` ne répondait pas. La pile légère (§3.4) a ramené ce coût à
  moins d'une seconde, mais la paresse reste **nécessaire** : construire ce moteur
  peut déclencher le téléchargement de 90 Mo, qu'on ne veut pas au démarrage. **Ne pas « simplifier » en instanciant directement.**
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

### 3.3 bis Ingestion des documents — **deux chemins, pas un**

C'est la confusion la plus facile à faire, et elle mène à croire un format
supporté là où il ne l'est pas :

| chemin | code | formats | ce qu'il produit |
|---|---|---|---|
| **RAG / fiches** | `RAGEngine._extract_text_from_path` | les 12 de `SUPPORTED_EXTENSIONS` | des chunks de texte pour la recherche |
| **module Docs** | `docanalysis.load_document_streaming` | **PDF seulement** | un document paginé (`n_pages`, aperçu, résumé) |

Le second appelle `pypdf.PdfReader` sans condition **parce qu'il compte des
pages** : l'étendre n'est pas ajouter une branche, c'est décider ce que « page »
veut dire pour un classeur. Son `accept` côté frontend annonçait dix types pour
n'en accepter qu'un ; ramené à `.pdf` le 2026-08-24.

**IMPÉRATIF — une seule liste d'extensions.** `SUPPORTED_EXTENSIONS`
(`core/rag.py`) est la source ; `modules/settings/router.py:_SUPPORTED_EXT`
l'importe, le message du 400 de l'upload en est dérivé, et le frontend en tient
un miroir unique (`EXTENSIONS_ACCEPTEES` dans `ModuleBar.tsx`, d'où sortent
`accept` **et** le filtre de `uploadFiles`). Il y en avait trois côté backend et
deux côté frontend : l'oubli le plus probable produit le pire symptôme — un
fichier accepté que le moteur ne sait pas lire s'indexe **à zéro chunk, en
silence**.

`.pptx`/`.xlsx` ajoutés le 2026-08-24 (python-pptx, openpyxl : `py3-none-any`,
aucune extension compilée, +6,6 Mo, zéro transitif nouveau). `.docx` était déjà
lu — ce qui manquait était le contenu de ses **tableaux**, que `doc.paragraphs`
n'inclut pas. Pas de conversion externe : ni LibreOffice, ni Office, ni binaire
appelé. Et **pas** de `.doc`/`.ppt`/`.xls` — aucune des trois bibliothèques ne lit
l'OOXML pré-2007, les accepter donnerait une erreur à l'ouverture au lieu d'un
refus à l'upload.

Convention des extracteurs : paquet **absent** → avertissement + chaîne vide
(dégradation, le paquet livré peut l'avoir perdu) ; fichier **illisible** →
l'exception remonte, comme `.pdf` depuis toujours. Ne pas confondre les deux :
l'un est une installation incomplète, l'autre un mauvais fichier.

**Les images (`.png`/`.jpg`/`.jpeg`/`.webp`) ont, depuis le 2026-09-01, un
troisième comportement — et lui non plus n'est pas uniforme entre les deux
usages de `_extract_text_from_path` :**

- **`index_file`** (l'indexation RAG) bascule vers `RAGEngine._texte_image`,
  qui appelle un modèle vision (`LLMEngine.describe_image`, choisi par
  `core.models.premier_modele_vision_disponible()` — FLM d'abord, sinon
  l'Ollama de `config.yaml:vision.ollama_model`, défaut `moondream`) pour
  produire une description ET transcrire le texte visible, remplaçant le
  placeholder muet d'avant.
- **`read_file_text`/`read_pdf_text`** (lecture ad hoc d'un fichier — `/skills/
  résumé`, l'aperçu d'upload de Réglages) restent sur `_extract_text_from_path`
  et son placeholder statique. **C'est un choix de périmètre assumé, pas un
  oubli** : seule l'indexation appelle un modèle vision.

Dégradation à trois niveaux, même esprit que les extracteurs ci-dessus mais un
cran de plus : aucun `llm` injecté (scripts, tests légers), aucun modèle
vision disponible, ou l'appel échoue (timeout, réponse vide) → le placeholder,
jamais une exception. Verrouillé par `test_vision_images.py`.

**Coût mesuré, à garder en tête** : `index_file` est appelé par fichier depuis
le flux de chargement du chat (`_stream_load_sse`) — attacher une image à une
conversation bloque donc ce flux le temps de l'appel vision. Mesuré sur ce
poste, modèle déjà chargé : ~2 s (Ollama/`moondream`) à 26 s
(`flm:qwen3vl-it:4b`, image simple) — jusqu'à 12 s pour `flm` sur une image plus
chargée (diagramme + formule). Rien ne borne l'attente en cas de panne autre
que les délais par défaut des clients (SDK `openai` : 600 s ; `ollama_client` :
300 s en lecture) — la dégradation ci-dessus finit donc par se produire, mais
pas vite.

**Qualité mesurée, pas supposée — `moondream` transcrit mais décrit mal.** Sur
un texte simple (« THALES 42 » seul), les deux providers transcrivent
correctement. Sur une image plus proche d'un cours réel (triangle annoté +
formule « AB/AC = AM/AN = 3/5 ») : `flm:qwen3vl-it:4b` transcrit le titre ET
la formule mot pour mot, en français ; `moondream` décrit la forme du triangle
mais **ne transcrit pas la formule** (« a list of numbers and letters ») et
répond en anglais à un prompt français. `moondream` reste le repli retenu
(seul modèle vision Ollama vérifié, se pull et tourne vite) mais son résultat
sur du texte structuré est plus faible que celui de `flm` — à garder en tête
avant de compter sur la transcription Ollama pour des formules.

**`_VISION_PROMPT` (`core/llm.py`) est délibérément COURT.** Mesuré sur
`moondream` : une formulation plus longue, énumérant titres/légendes/formules/
annotations entre parenthèses, fait dégénérer ce modèle — réponse VIDE
(`eval_count: 1`) ou boucle de répétition (1265 tokens de charabia pour la même
image). La forme courte est robuste sur les deux providers câblés ; ne pas
l'étoffer sans rejouer la mesure sur `moondream`.

### 3.7 Le cloud ne part jamais sans qu'on l'ait demandé

**IMPÉRATIF — une tâche qui n'est pas le tour de chat de l'utilisateur tourne en
LOCAL.** Elle ne part vers un fournisseur distant que sur un choix explicite
*pour cette tâche précise*, jamais en héritant de `modèle_actif` : celui-là est
un choix fait pour **répondre à un message**, pas un mandat sur tout ce que
l'instance fait en arrière-plan.

Ce que six sites faisaient avant le 2026-08-24, en lisant `ctx["modèle_actif"]` :
choisir Groq ou Gemini pour discuter suffisait à envoyer le contenu des fiches
(12 000 caractères pour `/skills/résumé`), celui des fichiers importés,
14 000 caractères de cours (flashcards), les réponses de kholle avec le contexte
mémoire, et le profil de révision — lacunes confirmées comprises. Deux autres
partaient en cloud **en dur** : la classification du palier Adaptatif (avant
chaque message) et la réflexion de l'agent de code.

Le contrat, dans `core/instance.py` :

| fonction | rôle |
|---|---|
| `modele_local_defaut()` | `providers.local` (le réglage) → `config.yaml` → `_DEFAULT_LOCAL_MODEL`. **Le seul point de lecture** : `self._llm._model` était lu en dur à 5 endroits, tous hors du réglage. |
| `modele_pour_tache(use_cloud, modele_cloud, cle_env)` | `False` → local ; `True` → le modèle **nommé pour la tâche**, si sa clé est là, sinon repli local. |
| `est_modele_cloud(id)` | préfixe comparé à `_FOURNISSEURS_CLOUD`, jamais la présence d'un « : » — `qwen2.5:7b` en contient un. **`flm` est LOCAL** (le NPU de la machine). |

Trois règles qui se déduisent mal :

- **`use_cloud=True` ne veut pas dire « le modèle du chat »** mais « le modèle
  décidé pour cette tâche ». Le module Docs avait le drapeau et visait quand même
  `modèle_actif` : le garde-fou existait et ne gardait rien.
- **Pas de `use_cloud` là où l'utilisateur ne peut pas le poser.** Résumé
  d'import, plan de révision (`GET`, sans corps) : toujours local. Un drapeau sans
  interface pour le poser est une option que personne ne peut atteindre.
- **Jamais `None` comme modèle de tâche de fond** : `LLMEngine` retombe alors sur
  `config.yaml`, donc contourne le réglage sans que le site d'appel le sache.

**Volontairement HORS de cette règle** : les paliers Medium/High de
l'orchestrateur, dont les défauts cloud (Groq, Gemini) sont le but assumé du
palier et sont modifiables dans l'interface — c'est un choix de l'utilisateur,
pas une tâche de fond. Et l'Atelier, qui a sa propre configuration
(`providers.actif` d'instance).

Verrouillé par `test_taches_locales.py`, qui pose le pire cas — `modèle_actif`
cloud **et** toutes les clés d'API présentes — avant chaque vérification.

### 3.4 Persistance

Aucune base de données côté application. Deux stockages :

- **Fichiers JSON** sous `backend/memory/` et `backend/history/`, via
  **`core/jsonstore.py` — IMPÉRATIF : jamais de `json.load`/`json.dump` direct.**
  Lecture en `utf-8-sig` (un BOM posé par PowerShell 5.1 rendait la mémoire de
  session invisible, puis le fichier était écrasé), écriture en `utf-8` sans BOM.
- **`core/vector_store.py`** (SQLite + numpy, cosinus par force brute) sous
  `backend/vector_db/` — `resolve_vector_dir()`, `$EPURE_VECTOR_DIR` — pour les
  trois collections vectorielles : `fiches` (RAG), `doc_analysis`, `history`.
  **IMPÉRATIF : un seul store, construit par `core/runtime.py` et INJECTÉ aux
  trois moteurs.** Ne pas en instancier un second, ni retourner aux attributs
  privés `rag._client`/`rag._ef` qui portaient ce partage avant : c'est ce qui
  rendait possible de brancher `core/rag.py` sur un nouveau stockage en laissant
  `core/docanalysis.py` et `core/history.py` sur l'ancien sans que rien ne le
  signale.

  **IMPÉRATIF : `onnxruntime` s'importe DANS `MoteurEmbedding.__init__`, jamais
  en tête de `core/embedding.py`.** La règle vient de `sentence_transformers`, qui
  coûtait 17,4 s et chargeait torch au seul import du module — comme
  `core/vector_store.py` importe la chaîne d'embedding et que `core/runtime.py`
  importe `core/vector_store.py`, un import en tête de fichier se payait au
  démarrage d'uvicorn. Le coût est tombé à **0,37 s** avec ONNX Runtime, et la
  règle ne change pas de nature pour autant : **la paresse du proxy ne couvre que
  la CONSTRUCTION des moteurs, jamais l'import de leurs dépendances.**

  **LA PILE A CHANGÉ LE 2026-08-26** — `sentence-transformers` (donc torch,
  transformers, scikit-learn, scipy) est remplacé par **`onnxruntime` +
  `core/wordpiece.py`**, sur le MÊME modèle `all-MiniLM-L6-v2`, dont le dépôt
  HuggingFace publie déjà l'export ONNX fp32.

  Ce qui a forcé la sortie n'est pas le poids mais un blocage dur, mesuré deux
  fois à huit minutes d'écart sur la machine ARM64 d'un destinataire : **Smart App
  Control y bloque durablement `sklearn/utils/_isfinite`**, que
  `sentence-transformers` importe sans condition à son chargement — pour un
  `cos_sim` que `core/vector_store.py` n'appelle jamais, puisqu'il calcule son
  cosinus en numpy. `pip install` réussissait, l'import plantait. Et
  `scikit-learn` est une dépendance **inconditionnelle de toutes** les versions de
  `sentence-transformers` (vérifié de la 2.7.0 à la 6.0.0) : changer de version
  n'était pas une issue.

  **Les vecteurs sont les mêmes, et c'est mesuré sur l'index réel** : les
  180 chunks déjà stockés dans `vector_db/` — calculés par
  `sentence-transformers` — se recalculent au **cosinus 1.000000** (écart absolu
  maximal 2,1e-07) avec le nouveau moteur. **Aucune réindexation.**

  Trois points à ne pas défaire :

  - **`onnxruntime` est déclaré en DIRECT dans `requirements.txt`.** Il arrivait
    par `faster-whisper` et `piper-tts`, tous deux retirés des paquets ARM64
    (`HORS_PAQUET_PIP_ARM64`) : sans déclaration, la pile d'embedding aurait
    dépendu de paquets vocaux absents sur l'architecture même qui a motivé le
    chantier, et le poste de dev — où la voix est installée — n'aurait rien pu
    voir. C'est mot pour mot l'incident `websockets`/`uvicorn[standard]` (§8).
    Verrouillé par `test_dependances_declarees.py`.
  - **Le tokeniseur est en Python pur**, et pas `tokenizers`. Son `.pyd` n'est pas
    signé, c'est-à-dire la catégorie exacte de binaire que Smart App Control
    bloque — et le blocage se décide **par fichier**, sur réputation : les `.pyd`
    de numpy, non signés eux aussi, passent sur cette machine ; celui de
    scikit-learn non. On ne peut donc pas *raisonner* qu'un binaire non signé
    passera. Les trois binaires d'`onnxruntime`, eux, sont signés
    `CN=Microsoft Corporation` — vérifié sur la machine cible. Parité du
    tokeniseur prouvée identifiant par identifiant sur 200 échantillons
    (`test_wordpiece.py`, table figée : la CI la tient sans installer
    `tokenizers`).
  - **`core/embedding_install.py` a changé de nature, pas de rôle.** Il
    n'installe plus de paquets — il télécharge les **90 Mo de poids** du modèle
    (`urllib` + sha256, `.part` puis renommage atomique), exactement comme
    `core/voice.py` fait des 76 Mo de Piper. Le contrat HTTP est inchangé :
    `GET /rag/capabilities`, `POST /rag/install`, 503 porteur d'un état,
    `EPURE_EMBEDDING_AUTOINSTALL=0` pour couper. `TAILLE_ESTIMEE_MO` est
    désormais **dérivée** des tailles déclarées (91) au lieu d'être écrite à la
    main (elle disait 2000 pour 198 Mo de wheels réelles).

  Poids mesuré du changement : **198,3 Mo de wheels → 14,1 Mo**, et
  **850,7 Mo retirés du disque** pour ~41,7 Mo ajoutés. Le contournement
  `torch --index-url download.pytorch.org` construit pour ARM64 disparaît avec
  torch.

  chromadb a été retiré le 2026-08-13 (`docs/remplacement-vectoriel.md`) : aucune
  wheel Windows ARM64, et une grappe — `grpcio`, `kubernetes`, `opentelemetry-*` —
  qu'il déclarait en dépendances directes, donc impossible à écarter tant qu'il
  restait. **Son extra, lui, avait été oublié** : `uvicorn[standard]` emportait la
  seule implémentation WebSocket de l'arbre, et tout `/ws/*` est mort dans un
  paquet livré dix jours plus tard — cf. §8 et la séquelle en fin d'étape D du
  document. Une carte de dépendances inverse doit inclure les extras.

  `backend/chroma_db/` peut encore exister sur le disque : c'est l'ancien index,
  gardé le temps d'un usage réel du nouveau (étape C.4), et non une seconde base
  vivante.

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
- `resolve_embedding_dir()` — `$EPURE_EMBEDDING_DIR`, sinon
  `<backend>/embedding_model`. Jumeau du suivant et **pour les mêmes raisons** :
  cache de modèle (90,4 Mo d'ONNX + un vocabulaire, téléchargés au premier usage
  et vérifiés par sha256), donc détourné par `_test_env` mais **absent** de
  `REAL_DIRS`. Dossier séparé de `piper_models` et non un sous-dossier : les deux
  caches n'ont pas le même sort dans un paquet ARM64, où la voix est retirée de
  l'installation alors que l'embedding y fonctionne.
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
ces imports. `backend/_test_env.py` pose les **sept** variables sur des
temporaires uniques pour la session — `backend/modules/` et
`frontend/src/modules/` y sont **copiés** (sans `_backups`) pour que les tests
voient un arbre réaliste. C'est ce qui rend `DELETE /settings/modules/{id}`
testable : son `rmtree` frappe la copie. `EPURE_MODELS_DIR`, `EPURE_EMBEDDING_DIR`,
`EPURE_VECTOR_DIR` et `EPURE_WEB_DIR` sont posés sur des temporaires **vides**,
pour des raisons distinctes : copier 76 Mo de modèle vocal ou 90 Mo de modèle
d'embedding n'aurait aucun sens et aucun test ne les lit (détournés seulement pour
qu'un test construisant `PiperEngine` ou `MoteurEmbedding` par accident ne tire
rien dans les caches réels — et, pour l'embedding, avec
`EPURE_EMBEDDING_AUTOINSTALL=0` par-dessus) ; `frontend/dist/` est vidé pour le
**déterminisme** —
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

**Ce que `LLMEngine.stream()` yielde** : du `str` pour le texte, et des **dicts
sentinelles** pour le reste — `{"__stats__": True, …}` (tokens et durées) et
`{"__reasoning__": True, "content": …}` (raisonnement du modèle, Ollama seul).
**IMPÉRATIF : tout consommateur filtre par `isinstance(item, str)` avant de
concaténer.** Les douze sites d'appel le font ; le seul qui ne le faisait pas
(`_stream_résumé_sse`) sérialisait `__stats__` comme un token depuis toujours, ce
qui collait un « [object Object] » à la fin de chaque résumé. Une sentinelle de
plus ne doit pas pouvoir se retrouver dans du texte.

Côté WebSocket de chat, le raisonnement a son propre type — `{"type": "reasoning",
"content": …}`, même forme que `{"type": "token", …}`. Il **n'entre pas** dans
`accumulated`, donc pas dans `history`, donc pas dans le prompt du tour suivant :
c'est ce que `test_raisonnement_stream.py` vérifie explicitement.

**Bascule `raisonnement`** — `stream(..., raisonnement: bool = True)`, réglage de
session (`memory` → clé `raisonnement`, `PATCH /context/settings`, toggle dans le
panneau Compétences). Le défaut `True` est le comportement historique, donc les
onze autres appelants n'ont rien à passer. Les deux moteurs locaux **ne se
pilotent pas de la même façon**, et c'est mesuré, pas déduit :

| | désactiver | activer |
|---|---|---|
| **Ollama** | `think=False` — ignoré proprement par un modèle sans raisonnement | **ne rien passer.** `think=True` → **400** `"qwen2.5:7b" does not support thinking`, y compris sur le modèle par défaut de `config.yaml` |
| **FLM** (`/v1`) | `extra_body={"think": False}` | `extra_body={"think": True}` — toléré même par `lfm2:1.2b`, qui ne pense pas |

Deux pièges propres à FLM : **omettre le flag ne veut pas dire « défaut du
modèle » mais « garder la valeur du dernier appel »** (état collant côté serveur,
mesuré) — donc toujours le passer, dans les deux sens ; et il passe par
`extra_body`, le SDK `openai` levant sur un paramètre inconnu. Les fournisseurs
cloud ne reçoivent rien : leur bascule n'a pas été mesurée.

**Le raisonnement de FLM remonte aussi**, depuis le 2026-08-24 et sous la même
sentinelle `__reasoning__` — donc le même `{"type": "reasoning"}` et le même bloc
repliable, sans une ligne de frontend en plus. Le champ s'appelle
**`reasoning_content`** (pas `reasoning`), il est atteignable en attribut bien que
non modélisé par le SDK (`getattr`, jamais un accès direct), et **le premier chunk
le porte VIDE** : tester la vérité, pas la présence. Mesuré : premier contenu à
91,8 s avant, premier affichage à 5,3 s après — le silence était plus long ici
que sur Ollama, sur le chemin NPU censé être le rapide.

**Réservé à `flm`.** `deepseek` publie aussi un `reasoning_content` et le remonter
serait probablement juste, mais ça n'a pas été mesuré — le vérifier veut dire
appeler une API payante. Lever la garde tiendra en retirant le test sur
`provider` ; rien d'autre à changer.

**Ce que deux mesures trop étroites ont coûté**, et c'est l'enseignement à garder :
sondé sur `qwen3.5:4b` seul, FLM « ne séparait pas le raisonnement du contenu ».
Vrai de ce modèle, faux de `qwen3:4b`. **Un modèle sondé ne dit rien de la
famille** — même piège que le §0 de `docs/remplacement-vectoriel.md` a été écrit
pour éviter, rejoué sur les modèles au lieu des wheels.

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

**L'Atelier est désactivable, pour le paquet distribué** (`docs/distribution-empaquetee.md`
étape B) — **désactivable, pas supprimable**. Deux interrupteurs, à poser ensemble
(`tools/faire_paquet.py` le fait) mais indépendants :

| Interrupteur | Effet |
|---|---|
| `EPURE_ATELIER=0` | 404 sur `/workshop*`, `/settings/test/*`, `/settings/gateway/*`, et fermeture de `/ws/workshop` avant `accept()`. Le 404 est posé **avant** le contrôle de token : un 401 révélerait que la route existe. |
| `VITE_ATELIER=0` | l'Atelier sort du **bundle**, pas seulement de l'écran. |

**IMPÉRATIF — ne pas supprimer `core/module_workshop.py` ni `core/module_validate.py`
d'un paquet.** `core/catalogue.py` importe sept symboles du premier, qui importe le second
au niveau module : les retirer casse `POST /settings/catalogue/{id}/install` et
`DELETE /settings/modules/{id}`, c'est-à-dire l'écran Réglages du destinataire, pas
l'Atelier.

Côté frontend, `src/atelier.ts` doit rester une **comparaison directe**
(`import.meta.env.VITE_ATELIER !== '0'`). Un `?.trim()` la rend non pliable par rolldown,
la branche morte reste atteignable, et un `Workshop-*.js` de 26,1 ko **contenant le code de
l'Atelier** part quand même dans le paquet — orphelin, mais sur le disque et lisible.
Verrouillé par `test_paquet.py`.

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
| Premier message lent après une pause, **même vers un fournisseur cloud** | Un appel au modèle **local** traînait sur le chemin du message (sélection des sections de profil dans `core/memory.py`) : 2,000 s fermes de timeout, et l'appel n'était pas annulé pour autant, donc Ollama continuait de charger 4,7 Go (mesuré 13,8 s à froid) en concurrence avec la requête cloud. Un `future.result(timeout=…)` **borne l'attente, pas le travail** : `shutdown(wait=False)` ne tue pas le thread, et le read-timeout du client Ollama est de 300 s. Ne rien mettre de bloquant sur ce chemin — verrouillé par `test_memory_sans_llm.py`. |
| Tout `/ws/*` répond **401** (chat, Atelier, dictée) alors que le token est bon | Lire la ligne de démarrage : « `No supported WebSocket library detected` ». `uvicorn` seul ne parle pas WebSocket — il lui faut `websockets` ou `wsproto` importable, sinon la requête d'upgrade est servie comme un GET HTTP, où le token de query param n'est pas lu. Le paquet en a manqué depuis le retrait de `chromadb`, qui la fournissait par son extra `uvicorn[standard]` — sur x64 comme sur ARM64, le poste de dev n'en gardant qu'un orphelin. `wsproto==1.3.2` est déclarée pour ça ; ne pas la retirer en la prenant pour un résidu. Verrouillé par `test_websocket_dependance.py`. |
| La recherche documentaire répond **500 « ImportError »** dans un paquet livré | La pile d'embedding n'y était pas installée et rien ne l'installait — la promesse « s'installe au premier usage » était de la prose. Depuis le 2026-08-23, `VectorStore.__init__` appelle `exiger_pile()` : préparation en tâche de fond, 503 avec état, `GET /rag/capabilities`. Ne pas remettre `pip` dans `PURGE_SITE_PACKAGES`, ne pas préchauffer le RAG sans le modèle. Cf. §3.4 et `test_embedding_install.py`. |
| Un binaire **non signé** bloqué par Smart App Control, dans un paquet que personne n'a choisi | Vu deux fois sur la même machine ARM64, à un jour d'intervalle : `sklearn/utils/_isfinite` (plus de recherche documentaire), puis `regex/_regex.pyd` (plus aucun import de fichier). Deux paquets, une seule cause : **l'application lançait elle-même `pip install sentence-transformers`** au premier usage, faisant entrer ~40 paquets non relus — dont la chaîne `sentence-transformers` → `transformers` → `regex`. Corriger un binaire puis attendre le suivant n'est pas une stratégie : depuis le 2026-08-26, **le chemin d'embedding n'exécute plus aucun sous-processus** et ne fait entrer que deux fichiers dont il connaît le sha256. Verrouillé par `AucuneInstallationALExecutionTest` (`test_embedding_install.py`). Le seul `pip` d'exécution qui subsiste est `POST /code/install`, du module de catalogue `code` — opt-in, nom de paquet tapé par l'utilisateur. |
| Une dépendance **porteuse** qui arrive par un paquet tiers finit par disparaître avec lui | Vu deux fois. `websockets` arrivait par l'extra `standard` d'`uvicorn`, déclaré par `chromadb` : son retrait a tué tout `/ws/*` dans un paquet livré, sur toutes les architectures, et le poste de dev n'a rien vu (il en gardait un orphelin). `onnxruntime` allait rejouer la même chose — installé en transitif par `faster-whisper`/`piper-tts`, tous deux exclus des paquets ARM64, alors qu'il porte désormais TOUT l'embedding. Règle : **ce dont on dépend directement est déclaré directement, même si c'est déjà installé.** « Installé » n'est pas « déclaré », et la différence n'apparaît que chez quelqu'un d'autre. Verrouillé par `test_dependances_declarees.py`. |
| Un `.ps1` sans BOM meurt sur une **cascade d'erreurs de parsing** loin de sa cause | `powershell.exe` (5.1) lit un `.ps1` sans BOM avec la page de code système — Windows-1252, pas UTF-8. Le tiret cadratin `—` (E2 80 94) et le filet `─` (E2 94 80) y produisent tous deux un **U+201D**, que PowerShell traite comme un délimiteur de chaîne : une chaîne ouverte par `"` peut donc être fermée par lui. Mesuré : 33 erreurs, la première annoncée ligne 253 sur une ligne strictement ASCII, et 0 erreur sous `pwsh 7`. **ASCII pur** dans tout `.ps1`/`.cmd` versionné (`test_encodage_scripts.py`). |
| `Expand-Archive` **s'imbrique** au lieu de remplacer, et tout réussit ensuite | `Expand-Archive -DestinationPath .` lancé depuis `epure\` n'écrase pas son contenu : il y crée `epure-main\`. Les étapes suivantes tournent alors sur l'ANCIEN code — `npm install` réussit, `faire_paquet.py` réussit, l'installation réussit, et le destinataire reçoit le paquet qu'il avait déjà. La pire forme d'échec : celle qui rend un succès. Extraire dans un **temporaire**, y trouver l'unique dossier de sommet, vérifier qu'il ressemble au dépôt, puis copier son CONTENU — jamais d'extraction dans le dossier de destination. Un piège de même nature guette une couche plus bas : `Copy-Item -Recurse` avec `-Destination <racine>\backend` crée `backend\backend` ; c'est `-Destination <racine>` qui fusionne. Verrouillé par `test_mise_a_jour.py`, cas de contrôle compris. |
| Du code passé à `python -c` arrive **amputé de ses guillemets** | Troisième piège de `powershell.exe` 5.1, après le cp1252 et le stderr. La ligne de commande d'un binaire natif est reconstruite selon `CommandLineToArgvW`, et 5.1 **n'échappe pas les `"` internes** d'un argument : `print("absent " + nom)` arrive `print(absent  + nom)`, donc `SyntaxError: '(' was never closed`. Reproduit sur x64, sans SAC — `pwsh` 7 n'a pas le défaut, ce qui explique qu'on ne le voie jamais en développement. L'échauffement de `tools/installer-epure.ps1` n'a donc **jamais fonctionné chez un destinataire** : il accusait Smart App Control, attendait 20 s, rejouait le même échec. Écrire le code dans un fichier temporaire et lancer `python fichier.py` (stdin marche aussi) ; jamais `-c`. Verrouillé par `EchauffementTest` (`test_installeur.py`). |
| Un script PowerShell s'arrête sur une commande qui a **réussi** | Sous `powershell.exe` (5.1), une redirection `2>&1` sur un binaire NATIF convertit chaque ligne de son stderr en `ErrorRecord`, et `$ErrorActionPreference = 'Stop'` en fait une erreur TERMINANTE — même quand le binaire sort en 0. Le `if ($LASTEXITCODE -ne 0)` écrit juste après n'est jamais atteint. `tools/dev-epure.ps1` mourait ainsi sur l'avertissement de taille de chunk de Vite, build réussi. Passer par `Invoquer-Externe`, qui relâche la préférence en portée de FONCTION. Verrouillé par `test_dev_epure.py`. |
| `TypeError: Cannot read properties of undefined (reading 'length')` dans un chunk minifié | Un état alimenté par `r.json() as {champ: T[]}` sur une réponse d'ERREUR : le champ est absent, l'état passe à `undefined`, le `.catch()` ne voit rien (le parse a réussi) et ça ne casse qu'au rendu suivant. Mesuré : dans un paquet livré, `GET /rag/files` répondait 500 (la pile d'embedding n'y était pas installée, et le premier accès au moteur RAG la construit) — le panneau fichiers du module Docs était mort d'avance. Normaliser à CHAQUE frontière `.json()` (`liste()`/`categories()`/`dico()` dans `ModuleBar.tsx`), et `Array.isArray` plutôt que `?? []`, qui laisse passer une chaîne ou un objet. Un `cloud: {}` est TRUTHY : `?? {…}` ne le rattrape pas. Verrouillé par `frontend/src/components/ModuleBar.test.tsx`. |
| Un modèle à raisonnement (qwen3) reste **muet une minute** puis lâche trois mots | Le raisonnement arrive dans un champ **séparé** du flux Ollama (`chunk.message.thinking`), pas en balises `<think>`, et **sans qu'on le demande** — aucun argument `think` n'est passé. `_stream_ollama` ne lisait que `content` et faisait `if content: yield content` : un chunk de raisonnement a `content == ""`, donc rien n'était yieldé. Mesuré sur `qwen3:8b` : **584 tokens en 78 s, premier caractère visible à 76,5 s**, pour `17 x 23 = 391.` — et `num_predict` consommé de façon invisible. Corrigé le 2026-08-24 : sentinelle `__reasoning__` → `{"type": "reasoning"}` sur `/ws/chat` → bloc repliable dans le chat (premier affichage à 7,8 s, mesuré). Ne PAS passer `think=True` : inutile pour les modèles qui pensent, et ça modifierait l'appel pour ceux qui ne pensent pas. Côté FLM il n'y a rien à récupérer — mesuré sur `qwen3.5:4b` via `/v1/chat/completions`, le delta ne porte que `role`/`content`. |
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
`backend/history/`, `backend/chroma_db/`, `backend/vector_db/`, `backend/doc_uploads/`,
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
