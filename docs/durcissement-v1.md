# Durcissement v1 — plan d'exécution

**Statut** : à exécuter. **Périmètre** : sécurité + intégrité des données, sans
changement d'architecture. **Branche** : `hardening/v1`, un commit par lot.

Chaque item a été **vérifié empiriquement** (exécution du code, pas lecture
seule). Les numéros de ligne correspondent à l'état du dépôt au 7 août 2026.

**Décisions actées avec Ilyann** avant écriture de ce plan :

| Question | Décision |
|---|---|
| Accès depuis un autre appareil ? | Indécis → bind `127.0.0.1` par défaut, réouverture explicite via `EPURE_BIND` |
| Moteur passerelle / LiteLLM ? | Conservé (peut être remplacé plus tard) → on sécurise, on ne supprime pas |
| `/admin/open` ? | Conservé (utile) → `Popen` en liste + chemin confiné |
| Format ? | Un lot = un commit, sur une branche dédiée |

---

## Lot 1 — Surface d'exposition réseau

*Aucune décision d'architecture. ~30 min. À faire en premier : ferme la voie
d'attaque réelle (page web → DNS rebinding → token → RCE).*

### 1.1 — Le backend écoute sur toutes les interfaces

**`epure_tray.py:113-115`**

```python
uvicorn_cmd = [
    sys.executable, "-m", "uvicorn", "main:app",
    "--host", "0.0.0.0", "--port", "8000",
]
```

Sur le wifi de la prépa, le port 8000 est visible de tout le réseau.

**Correctif** — lire l'hôte dans l'environnement, défaut loopback :

```python
_BIND = os.environ.get("EPURE_BIND", "127.0.0.1").strip() or "127.0.0.1"
uvicorn_cmd = [
    sys.executable, "-m", "uvicorn", "main:app",
    "--host", _BIND, "--port", "8000",
]
```

Documenter `EPURE_BIND` dans `README.md` (tableau des variables) et
`backend/.env.example`, avec l'avertissement : ouvrir au LAN expose une API dont
un seul token protège l'exécution de commandes.

Ne pas toucher à `backend/Dockerfile:35` (`--host 0.0.0.0`) : dans un conteneur
c'est correct, l'exposition est pilotée par le mapping de ports.

**Non-régression** : `python epure_tray.py` → l'UI répond sur
`http://localhost:5173` ; `curl http://<ip-lan>:8000/health` depuis un autre
poste → connexion refusée. Avec `EPURE_BIND=0.0.0.0`, la même requête répond.

### 1.2 — Aucun `TrustedHostMiddleware` → DNS rebinding sur `/pair`

**`backend/main.py:73-93`** — `app = FastAPI(...)` puis le middleware de token ;
aucun contrôle de l'en-tête `Host`.

**`backend/main.py:643-649`** :

```python
host = request.client.host if request.client else ""
if not is_local_client(host):
    raise HTTPException(status_code=403, ...)
return {"token": get_api_token()}
```

La vérification d'IP est correcte en soi (pas de lecture de `X-Forwarded-For`,
`client` absent → 403). Mais `/pair` est exempt d'auth, et une page attaquante
dont le domaine résout vers `127.0.0.1` devient *same-origin* : CORS ne
s'applique pas, `request.client.host` vaut bien `127.0.0.1`, le token part.
Ensuite `/admin/open` (lot 2) donne l'exécution de commande.

**Correctif** — ajouter, **avant** le middleware de token :

```python
from fastapi.middleware.trustedhost import TrustedHostMiddleware

_ALLOWED_HOSTS = [
    h.strip() for h in
    os.environ.get("EPURE_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",")
    if h.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)
```

Attention à l'ordre : Starlette applique les middlewares du dernier ajouté vers
le premier. `TrustedHostMiddleware` doit être **la couche la plus externe**, donc
ajouté **après** `CORSMiddleware` et après `_require_api_token` dans le fichier.
Vérifier ce point en test, pas au raisonnement.

Si `EPURE_BIND` est ouvert au LAN, l'utilisateur doit ajouter son IP/nom d'hôte à
`EPURE_ALLOWED_HOSTS` — le documenter au même endroit que `EPURE_BIND`.

**Non-régression** — nouveau `backend/test_auth_surface.py` :

- `GET /health` sans token → 200
- `GET /pair` avec `Host: localhost` depuis 127.0.0.1 → 200 et un token
- `GET /pair` avec `Host: attaquant.example` → 400 (rejet TrustedHost)
- `GET /models` sans token → 401, `avec` token → 200
- `GET /instance/config` → la réponse ne contient ni `auth`, ni la sous-clé
  `atelier.gateway.api_key`

Utiliser `fastapi.testclient.TestClient` (nécessite `httpx` — l'ajouter à
`requirements.txt` et à l'étape `pip install` de `ci.yml`).

### 1.3 — Le healthcheck Docker échoue en permanence

**`backend/Dockerfile:32-33`**

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=5 \
    CMD curl -fsS http://localhost:8000/openapi.json > /dev/null || exit 1
```

`/openapi.json` n'est pas dans `_AUTH_EXEMPT_PATHS` → 401 → `curl -f` échoue →
conteneur marqué `unhealthy` en permanence, et `depends_on: condition:
service_healthy` ne se déclenche jamais.

**Correctif** : `http://localhost:8000/health`.

**Non-régression** : `docker compose up --build`, puis `docker compose ps` →
`healthy` sous 5 min.

### 1.4 — Le token en clair dans l'URL des WebSockets

**`frontend/src/api.ts:87`**

```ts
return `${WS_BASE}${path}${token ? `${sep}token=${encodeURIComponent(token)}` : ''}`
```

L'URL complète part dans les access-logs uvicorn, donc dans `epure_tray.log`
(8 Mo sur le disque, non chiffré).

**Correctif minimal, sans changer le protocole** : désactiver l'access-log
uvicorn pour les WebSockets, ou plus simplement lancer uvicorn avec
`--no-access-log` dans `epure_tray.py` (le tray a déjà son propre log applicatif).

**À ne pas faire dans ce lot** : passer le token en sous-protocole WebSocket.
C'est la bonne solution long terme mais elle touche les 3 endpoints WS et le
client ; hors périmètre du durcissement. À noter comme dette.

---

## Lot 2 — Exécution de commandes

*Trois `shell=True` atteignables depuis l'API. ~1 h.*

### 2.1 — Injection de commande dans `/admin/open`

**`backend/modules/admin/router.py:92-99`**

```python
@router.get("/open")
async def admin_open(path: str):
    try:
        import subprocess
        subprocess.Popen(f'explorer /select,"{path}"', shell=True)
```

`GET /admin/open?path=x" %26 calc.exe %26 "` → `cmd.exe` exécute `calc.exe`. Le
guillemet ferme la chaîne, `&` enchaîne une commande.

**Correctif** — endpoint conservé (décision d'Ilyann), mais sans shell et avec
confinement :

```python
from pathlib import Path
from core.instance import fiches_root
from core.paths import resolve_workspace

_OPENABLE_ROOTS = ...  # fiches_root(), resolve_workspace(), backend/doc_uploads

@router.get("/open")
async def admin_open(path: str):
    target = Path(path).expanduser().resolve()
    if not any(target.is_relative_to(r.resolve()) for r in _OPENABLE_ROOTS):
        raise HTTPException(status_code=403, detail="Chemin hors des dossiers autorisés")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    if os.name == "nt":
        subprocess.Popen(["explorer", f"/select,{target}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target.parent)])
    return {"ok": True}
```

Note : `explorer` exige `/select,<chemin>` en **un seul argument** — le découpage
`["explorer", "/select,", path]` ne fonctionne pas. Vérifier manuellement sous
Windows, ce point ne se teste pas en CI Linux.

Remonter l'`import subprocess` en tête de fichier (il est actuellement dans le
corps de la fonction).

**Non-régression** : ouvrir une fiche depuis le module Admin → l'explorateur
s'ouvre sur le bon fichier. `?path=C:\Windows\System32\calc.exe` → 403.
`?path=x" %26 calc %26 "` → 403, et aucun processus lancé.

### 2.2 — RCE persistante via `atelier.gateway.start_command`

**`backend/core/module_workshop.py:411-424`**

```python
cmd = cfg["start_command"]
...
subprocess.Popen(
    cmd, shell=True, cwd=str(Path.home()),
    stdout=subprocess.DEVNULL, ...
)
```

`start_command` est une chaîne libre écrite par `PUT /instance/config`
(`core/instance.py` ne protège que `instance_id` et `auth`). Deux requêtes
suffisent : poser la commande, appeler `POST /settings/gateway/start`.

**Correctif** — le moteur passerelle est conservé, donc on garde la
fonctionnalité mais on retire le shell :

```python
import shlex

_GATEWAY_ALLOWED_BINS = {"litellm", "python", "python3", "py", "npx", "uv", "uvx"}

argv = shlex.split(cmd, posix=(os.name != "nt"))
if not argv:
    return {"ok": False, "raison": "Commande de démarrage vide."}
head = Path(argv[0]).stem.lower()
if head not in _GATEWAY_ALLOWED_BINS:
    return {"ok": False, "raison":
            f"Binaire non autorisé : {argv[0]}. Autorisés : {sorted(_GATEWAY_ALLOWED_BINS)}"}
subprocess.Popen(argv, shell=False, cwd=str(Path.home()), ...)
```

`shlex.split(..., posix=False)` sous Windows préserve les chemins avec
antislashs. Tester avec la commande LiteLLM réelle d'Ilyann.

**Non-régression** : `POST /settings/gateway/start` avec la commande LiteLLM
habituelle → la passerelle démarre, `gateway_reachable` passe à True. Avec
`start_command = "calc.exe"` → refus explicite, rien lancé.

### 2.3 — Injection via `module_id` dans `open_terminal`

**`backend/core/module_workshop.py:1213-1232`**

```python
bat.write_text("@echo off\r\n" f'cd /d "{sdir}"\r\n' f"call {claude_cmdline}\r\n", ...)
subprocess.Popen(f'start "Atelier {module_id}" cmd /K "{bat}"', shell=True, env=env)
...
subprocess.Popen(["x-terminal-emulator", "-e",
                  f'bash -c \'cd "{sdir}"; {claude_cmdline}; exec bash\''], env=env)
```

`sdir` et `module_id` proviennent d'un id non validé (cf. 3.1). Sous POSIX, un
nom de dossier contenant `"; id; "` est légal.

**Correctif** : appliquer `_ID_RE` en entrée d'`open_terminal` (couvert par 3.1
si la validation est posée dans `_staging_dir`), et pour la branche POSIX
remplacer l'interpolation par `shlex.quote(sdir)`. La branche Windows reste en
`shell=True` (`start` est une commande interne de `cmd`) mais devient sûre dès
que `module_id` est contraint à `[a-z][a-z0-9_]{1,30}`.

**Non-régression** : ouvrir un terminal Atelier sur le module `hello` → session
`claude` dans le bon dossier.

---

## Lot 3 — Confinement de chemin

*Trois écritures de fichier pilotées par le client. ~45 min.*

### 3.1 — Traversée `..` dans l'id de module de l'Atelier

**`backend/core/module_workshop.py:195`**

```python
def _staging_dir(module_id: str) -> Path:
    return _modules_safe_path(f"_staging/{module_id}")
```

`_ID_RE` n'est validé que dans `ModuleManifest._check_id` et dans `prepare()`
(l.335). `_staging_dir` est appelé par `reject`, `read_staging`,
`validate_staging`, `grant_read`, `aider_converse`, `open_terminal`,
`_write_blocks_from_text` — sans validation. Sur `/ws/workshop`, l'id vient de
`msg.get("id", "")` (`main.py:566`, `591`, `607`).

**Vérifié** : `(MODULES_DIR / "_staging/../chat").resolve()` →
`backend/modules/chat`, et `is_relative_to(MODULES_DIR)` renvoie **True**. Le
garde-fou ne voit rien parce que la cible reste sous `modules/`.

Scénario : `{"type":"generate","id":"../chat"}` → le LLM écrit directement dans
un module core, hors staging, sans validation ni approbation.
`{"type":"reject","id":"../hello"}` → `shutil.rmtree` du module.

**Correctif** — valider à la source :

```python
def _staging_dir(module_id: str) -> Path:
    mid = (module_id or "").strip()
    if not _ID_RE.match(mid):
        raise SecurityError(f"Identifiant de module invalide : {mid!r}")
    return _modules_safe_path(f"_staging/{mid}")
```

Vérifier que `SecurityError` remonte en 400 côté HTTP et en message d'erreur
côté WebSocket (pas en 500 opaque).

**Non-régression** — nouveau `backend/test_workshop_paths.py` :

- `_staging_dir("hello")` → `.../modules/_staging/hello`
- `_staging_dir("../chat")`, `_staging_dir("..")`, `_staging_dir("_staging")`,
  `_staging_dir("a/b")`, `_staging_dir("")`, `_staging_dir("Chat")` → `SecurityError`
- le module `chat` existe toujours après une tentative de `reject("../chat")`

### 3.2 — Écriture arbitraire via le nom de fichier uploadé (docs)

**`backend/modules/docs/router.py:82-87`**

```python
@router.post("/docanalysis/upload")
async def docanalysis_upload(file: UploadFile = File(...)):
    _DOC_UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = _DOC_UPLOADS / (file.filename or "upload.pdf")
    content = await file.read()
    dest.write_bytes(content)
```

`filename` est contrôlé par le client. `Path("/a") / "/etc/passwd"` renvoie
`/etc/passwd` : un nom absolu remplace la base. `../../x` sort du dossier.

**Correctif** :

```python
name = Path(file.filename or "upload.pdf").name or "upload.pdf"
dest = (_DOC_UPLOADS / name).resolve()
if not dest.is_relative_to(_DOC_UPLOADS.resolve()):
    raise HTTPException(status_code=400, detail="Nom de fichier invalide")
```

### 3.3 — Même faille sur l'upload de fiches, avec escalade vers le token

**`backend/modules/settings/router.py:363-370`**

```python
filename = upload.filename or "upload.bin"
ext = Path(filename).suffix.lower()
if ext not in _SUPPORTED_EXT:
    continue
dest = _fiches_dir / filename
content = await upload.read()
dest.write_bytes(content)
```

`.json` est dans `_SUPPORTED_EXT` (l.49). Un upload nommé
`../../backend/memory/instance_config.json` **réécrit la configuration
d'instance, donc le token d'API** — l'attaquant choisit sa propre valeur.

**Correctif** : identique à 3.2, `Path(filename).name` + contrôle
`is_relative_to(_fiches_dir.resolve())`. Ne pas retirer `.json` de
`_SUPPORTED_EXT` (il sert légitimement aux fiches).

Vérifier le même motif à `settings/router.py:271` (`ext not in _SUPPORTED_EXT`),
qui semble être un second point d'entrée.

**Non-régression (3.2 + 3.3)** — nouveau `backend/test_upload_paths.py` avec
`TestClient` : upload nommé `fiche.pdf` → écrit dans le bon dossier ; nommé
`../../evil.json`, `/etc/evil.json`, `..\\..\\evil.json` → 400, et
`backend/memory/instance_config.json` inchangé (comparer le hash avant/après).

---

## Lot 4 — Intégrité des données

*Aucun attaquant requis : c'est de la perte de données en usage normal. ~1 h 30.*

### 4.1 — `write_json` n'est pas atomique

**`backend/core/jsonstore.py:48`**

```python
p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
```

`write_text` tronque le fichier puis le réécrit : un lecteur concurrent voit du
vide ou du JSON partiel. `read_json` (l.34-37) attrape l'exception et renvoie
`default`, que le moteur réécrit ensuite — **effacement silencieux**. C'est le
même mécanisme que l'incident du BOM documenté en tête du module : l'encodage a
été corrigé, l'atomicité non.

**Mesuré** sur le code réel, 8 threads × 30 écritures : **106 lectures ont
observé un fichier corrompu**.

**Correctif** — le pattern existe déjà dans le dépôt,
`core/quota_tracker.py:80-83` :

```python
tmp = p.with_suffix(p.suffix + ".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
tmp.replace(p)   # atomique sur NTFS et POSIX
```

Une fois `jsonstore` corrigé, supprimer le tmp+replace local de
`quota_tracker.py` : un seul chemin d'écriture.

### 4.2 — Read-modify-write sans verrou

FastAPI exécute les handlers synchrones dans un pool de threads, et le code
lance en plus des `Thread` explicites (consolidation depuis
`modules/kholle/router.py:269` et `modules/chat/router.py:539`). Les écritures
sont réellement concurrentes.

Sites concernés :

| Fichier | Motif |
|---|---|
| `core/flashcards.py:97-118` | `update_carte` : charge, modifie une carte, réécrit tout |
| `core/history.py:93-95` | `conversations.insert(0, entry)` puis réécriture |
| `core/memory.py:161-164` | profil |
| `core/orchestrator.py:409-418` | presets |
| `core/consolidation.py:29-31` | log de consolidation |
| `core/admin.py:237-247` | log admin |

**Mesuré** : 240 écritures attendues, **2 conservées**.

**Correctif** — verrou par chemin, dans `jsonstore` :

```python
from threading import RLock
_locks: dict[str, RLock] = {}
_locks_guard = RLock()

def _lock_for(p: Path) -> RLock:
    key = str(p.resolve())
    with _locks_guard:
        return _locks.setdefault(key, RLock())

@contextmanager
def transaction(path, default):
    """Charge, cède la main, réécrit — sous verrou. Le seul chemin correct
    pour un read-modify-write sur un JSON de runtime."""
    p = Path(path)
    with _lock_for(p):
        data = read_json(p, default)
        yield data
        write_json(p, data)
```

Puis convertir les 6 sites ci-dessus :

```python
with transaction(_INDEX_FILE, []) as conversations:
    conversations.insert(0, entry)
```

Note : ces verrous sont **intra-processus**. Ils ne protègent rien si uvicorn est
lancé avec `--workers > 1`. Le dépôt tourne en un seul worker
(`Dockerfile:35`, `epure_tray.py`, `start.ps1`) — **ne pas ajouter `--workers`
sans passer à un verrou de fichier** (`msvcrt.locking` / `fcntl.flock`). À
inscrire en commentaire dans `jsonstore.py`.

### 4.3 — Aucun timeout sur Ollama

**`backend/core/llm.py:23`** — `ollama.Client(host=...)` sans `timeout`, puis
`.chat(...)` l.147. Un Ollama figé bloque un thread du pool indéfiniment ; côté
UI, le WebSocket de chat reste muet sans jamais émettre `done`.

**Correctif** : `ollama.Client(host=..., timeout=60)`, valeur lue dans
`config.yaml` (`model.timeout_s`, défaut 60). Même traitement pour
`core/admin.py:89`, qui utilise le client global brut au lieu du client
normalisé de `core/llm.py` — exporter `_ollama_client` depuis `core.llm` et
l'utiliser partout (`core/admin.py:31` et `:89`, `core/models.py:302`).

### 4.4 — Tests de non-régression

Nouveau `backend/test_jsonstore_concurrency.py` :

- 8 threads × 50 `transaction()` incrémentant un compteur → valeur finale
  exactement 400
- 4 écrivains + 4 lecteurs en boucle pendant 2 s → **zéro** lecture de JSON
  invalide (compter les retours `default` inattendus)
- `write_json` interrompu (simuler par un `json.dumps` qui lève) → le fichier
  d'origine est intact, seul le `.tmp` traîne

Ces tests doivent être **rapides** (< 5 s) pour rester dans le job CI léger.

---

## Lot 5 — Rendre les lots 1-4 durables

*Sans ce lot, les correctifs se dégradent silencieusement. ~30 min.*

### 5.1 — La CI n'exécute que 2 tests sur 6

**`.github/workflows/ci.yml:39-44`** : les tests sont lancés un par un, nommément.
`test_jsonstore.py`, `test_web_search.py` et `test_module_isolation.py` ne
tournent jamais.

**Correctif** — découverte automatique dans le job `backend` :

```yaml
- name: Dépendances minimales (pas de deps ML)
  run: pip install fastapi pydantic pyyaml python-dotenv httpx
- name: Tests unitaires (découverte automatique)
  working-directory: backend
  run: python -m unittest discover -s . -p 'test_*.py' -v
```

`test_modules_mount.py` doit rester exclu du job léger (il importe
`core.runtime` → torch, chromadb). Deux options : le renommer
`integration_modules_mount.py` — préférable, le pattern `test_*` cesse de le
capter — ou ajouter un `skipUnless(os.environ.get("EPURE_HEAVY_TESTS"))` en tête.
Choisir le renommage et mettre à jour le job `integration` en conséquence.

Ajouter `httpx` à `backend/requirements.txt` (nécessaire à `TestClient`, et déjà
requis par `test_module_isolation.py` qui échouerait sinon).

### 5.2 — La CI ne construit jamais le frontend

`ci.yml` fait `tsc --noEmit` mais pas `vite build`. Or `tsc --noEmit` ne couvre
pas les erreurs de bundling, et le TSX généré par l'Atelier atterrit dans
`frontend/src/modules/generated/` — inclus dans `tsconfig.app.json` (`"include":
["src"]`). Un module généré qui ne compile pas casse l'image frontend sans que
la CI le voie.

**Correctif** — après le type-check :

```yaml
- name: Build (bloquant)
  run: npm run build
```

### 5.3 — Ce que ce lot ne fait pas

`eslint` reste en `continue-on-error` et `"strict"` reste absent de
`tsconfig.app.json`. Les deux sont de vraies dettes, mais les résorber implique
de toucher ~94 erreurs sur tout le codebase : c'est un chantier séparé, pas du
durcissement. Le noter dans `CHANGELOG.md`.

---

## Hors périmètre — décidé, pas oublié

| Sujet | Pourquoi pas maintenant |
|---|---|
| **Contournements du gate AST** (14 vérifiés) | Une denylist AST est contournable par construction. Ajouter des règles donne une fausse confiance. La réponse est l'isolation worker — chantier suivant, pas du durcissement. |
| **Câbler `module_worker.py`** | Changement d'architecture (proxy `/<id>/*` + routes `/capabilities/*` + cycle de vie des workers). Mérite son propre plan. |
| **`force=true` sur `/workshop/{id}/approve`** | C'est un choix produit assumé (« j'active malgré la validation »). Tant que le gate n'est pas une frontière, le supprimer ne change rien au niveau de sécurité réel. À revoir avec l'isolation. |
| **Token en sous-protocole WS** | Touche les 3 endpoints WS + le client. 1.4 en traite le symptôme (les logs). |
| **`response_model` sur 103 endpoints** | Chantier de fond, indépendant. |
| **`"strict": true` + eslint bloquant** | ~94 erreurs à résorber. |
| **`piper_models/*.onnx` (76 Mo) tracké** | `.git` pèse 70 Mo. La purge par `git filter-repo` réécrit l'historique — à faire à froid, seul, pas au milieu d'un lot de correctifs. Ajouter `*.onnx` au `.gitignore` dès maintenant ne suffit pas à purger l'existant. |
| **`litellm.yaml:7` → `master_key: sk-epure-local`** | À sortir en variable d'environnement. Impact réel faible (passerelle locale), mais à faire dans le même passage que la purge git. |
| **`.claude/settings.local.json` versionné** | Expose l'arborescence `C:\Users\Ilyan\**` et pré-autorise `git push`/`git rm`/`git filter-repo`. À ajouter au `.gitignore` — trivial, mais c'est un `git rm --cached`, donc à grouper avec la purge. |

---

## Ordre d'exécution et critère de sortie

```
1  → commit  fix(security): API en loopback par défaut + TrustedHost + healthcheck
2  → commit  fix(security): suppression des shell=True atteignables depuis l'API
3  → commit  fix(security): confinement des identifiants de module et des uploads
4  → commit  fix(data): écritures JSON atomiques + verrou par fichier + timeout Ollama
5  → commit  ci: découverte automatique des tests + build frontend bloquant
```

**Critère de sortie du durcissement v1** : sur la branche `hardening/v1`,
`python -m unittest discover -s . -p 'test_*.py'` passe, `npm run build` passe,
et les 3 nouveaux fichiers de test (`test_auth_surface.py`,
`test_workshop_paths.py`, `test_upload_paths.py`,
`test_jsonstore_concurrency.py`) tournent en CI.

---

## Prompts pour Claude Code

Un prompt par lot, à donner tel quel. Le contexte long est dans `CLAUDE.md` à la
racine — Claude Code le lit automatiquement.

### Lot 1

> Lis `docs/durcissement-v1.md`, section « Lot 1 ». Crée la branche
> `hardening/v1` depuis `main`. Applique les items 1.1 à 1.4. Écris
> `backend/test_auth_surface.py` avec les 5 cas listés, en `unittest` +
> `fastapi.testclient.TestClient`, sur le modèle de `backend/test_safe_path.py`
> (script autonome, `sys.path.insert` en tête). Ajoute `httpx` à
> `backend/requirements.txt`. Documente `EPURE_BIND` et `EPURE_ALLOWED_HOSTS`
> dans le tableau des variables de `README.md` et dans `backend/.env.example`.
> Vérifie explicitement l'ordre des middlewares Starlette par un test, pas par
> raisonnement. Un seul commit :
> `fix(security): API en loopback par défaut + TrustedHost + healthcheck`.

### Lot 2

> Lis `docs/durcissement-v1.md`, section « Lot 2 ». Supprime les trois
> `shell=True` atteignables depuis l'API (2.1, 2.2, 2.3). `/admin/open` est
> conservé — ne le supprime pas. Le moteur passerelle est conservé — ne supprime
> pas `start_command`, sécurise-le. Attention : `explorer /select,<chemin>` doit
> être passé en **un seul argument**, sinon l'ouverture ne fonctionne pas sous
> Windows ; ce point n'est pas testable en CI Linux, signale-le-moi pour que je
> le vérifie à la main. Un seul commit :
> `fix(security): suppression des shell=True atteignables depuis l'API`.

### Lot 3

> Lis `docs/durcissement-v1.md`, section « Lot 3 ». Applique 3.1, 3.2, 3.3.
> Vérifie que `SecurityError` remonte en 400 côté HTTP et en message d'erreur
> typé côté WebSocket `/ws/workshop`, pas en 500 opaque. Écris
> `backend/test_workshop_paths.py` et `backend/test_upload_paths.py` avec tous
> les cas listés. Cherche d'autres appels à `_staging_dir` ou d'autres
> constructions `<dossier> / <nom venant du client>` que je n'aurais pas
> repérées, et traite-les de la même façon en me les signalant. Un seul commit :
> `fix(security): confinement des identifiants de module et des uploads`.

### Lot 4

> Lis `docs/durcissement-v1.md`, section « Lot 4 ». Rends `write_json` atomique,
> ajoute un contextmanager `transaction()` verrouillé dans
> `backend/core/jsonstore.py`, et convertis les 6 sites de read-modify-write
> listés. Supprime ensuite le tmp+replace local devenu redondant dans
> `core/quota_tracker.py`. Ajoute le timeout Ollama et unifie l'accès au client
> Ollama (4.3). Écris `backend/test_jsonstore_concurrency.py` — il doit tourner
> en moins de 5 s. Inscris en commentaire dans `jsonstore.py` que ces verrous
> sont intra-processus et interdisent `uvicorn --workers > 1` en l'état. Un seul
> commit : `fix(data): écritures JSON atomiques + verrou par fichier + timeout Ollama`.

### Lot 5

> Lis `docs/durcissement-v1.md`, section « Lot 5 ». Bascule le job `backend` de
> `ci.yml` sur `python -m unittest discover`, renomme `test_modules_mount.py` en
> `integration_modules_mount.py` et adapte le job `integration`. Ajoute
> `npm run build` bloquant au job `frontend`. Ne touche pas au
> `continue-on-error` d'eslint ni au tsconfig. Lance toute la suite en local et
> montre-moi la sortie avant de committer. Un seul commit :
> `ci: découverte automatique des tests + build frontend bloquant`.
