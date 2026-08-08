# Rapport de vérification — hardening/v1
Date : 2026-08-08
Dernier commit : `788b606` — docs: entree changelog isolation + deps CI

## Corrections appliquées
- uvicorn ajouté à ci.yml : **OUI** (§2.1). Correction non cosmétique, prouvée par
  l'exécution : `uvicorn` désinstallé du venv isolé, `test_module_isolation.py`
  passe de 5/5 à **3 échecs** (`le worker hello n'a pas démarré`). Le symptôme en
  CI aurait été un timeout de sonde, pas un `ModuleNotFoundError` lisible.
- autres deps ajoutées (§2.2) : **aucune**. Le venv rejouant la liste exacte de
  `ci.yml` fait passer les 144 tests sans autre manque.

## Vérifications
- §3.1 tests locaux (discover)      : OK — 144 tests, 15,278 s (21,83 s au mur), exit 0, `OK (skipped=2)`. `integration_modules_mount.py` **non ramassé** : la découverte charge exactement 10 modules (`test_auth_surface`, `test_command_exec`, `test_jsonstore`, `test_jsonstore_concurrency`, `test_module_isolation`, `test_module_validate`, `test_safe_path`, `test_upload_paths`, `test_web_search`, `test_workshop_paths`).
- §3.2 environnement CI rejoué      : OK — 144 tests, 15,464 s (28,11 s au mur), exit 0, `OK (skipped=2)`. Deps manquantes trouvées : **aucune au-delà d'`uvicorn` (§2.1)**. Isolation du venv contrôlée : `torch` absent, `sentence_transformers` absent, `uvicorn` et `chromadb` résolus depuis `…\epure-ci-venv\Lib\site-packages`.
- §3.3 npm run build (arbre local)  : OK — exit 0, `built in 2.21 s` (10,97 s au mur avec `tsc -b`, cache `node_modules/.tmp` purgé au préalable). Bundle : `index` 831,32 kB (gzip 222,40 kB), `RichMessage` 556,79 kB (gzip 170,10 kB).
- §4   git status vide après commits: OK — `git status --porcelain` vide après les trois commits (`10ff333`, `5f5ca9c`, `788b606`).
- §5.1 modules du clone conformes   : OK — liste réelle : `_atelier, admin, chat, code, docs, flashcards, hello, history, kholle, rangement, reviseur, settings` (12). Aucun des neuf modules de test.
- §5.2 generated = hello, rangement : OK — liste réelle : `hello, rangement`.
- §5.3 git status vide dans le clone: OK — sortie vide immédiatement après `git clone --no-local`. `.gitattributes` (commit `1a11c2f`) fait donc son office.
- §5.4 aucune donnée personnelle    : OK — les deux commandes ne produisent **aucune** sortie (`git log --all` sur `backend/history|memory|doc_uploads|chroma_db|workspace`, et `git rev-list --objects --all` filtré sur `TIPE|profile.json|conversations.json|instance_config|.env$`).
- §5.5 npm ci + build depuis le clone: OK — `npm ci` exit 0 en 23,42 s ; `npm run build` exit 0, `built in 2,61 s` (13,17 s au mur).
- §5.6 taille de .git               : **69,3 Mo** (l'ONNX de 76 Mo reste dans l'historique, non traité — connu).

## Anomalies rencontrées

1. **`.gitignore:1` est inopérant.** La ligne vaut `.claude/settings.local.json"`
   — guillemet double parasite en fin de motif. Ce qui masque réellement le
   fichier sur ce poste est le **gitignore global**
   (`C:\Users\Ilyan/.config/git/ignore:1:**/.claude/settings.local.json`), vérifié
   via `git check-ignore -v`. Conséquence : chez quelqu'un d'autre, le fichier
   recréé par Claude Code apparaîtra en `??`. **Pas une fuite** — le
   `git rm --cached` de `b18e9e0` a bien pris effet, `git ls-files .claude/` est
   vide et le clone froid ne contient pas le fichier. Non corrigé : hors du
   périmètre autorisé par la §2.

2. **Dérive entre la §1 du script et l'état réel du dépôt.** La §1 attendait
   `?? CLAUDE.md` et `?? docs/` ; les deux étaient déjà committés (`bdb2b7a`)
   avant le démarrage. Rien d'autre n'apparaissait, donc pas de condition
   d'arrêt. Effet : le `git add CLAUDE.md docs/` de la §4 n'a réellement staged
   que `docs/verif-hardening.md`.

3. **Le commit `feat(isolation)` ne met pas l'isolation en vigueur** — vérifié,
   pas supposé : `spawn_worker` n'est appelé nulle part hors tests, aucune route
   `/capabilities/*` n'existe, et `core/module_registry.py:95` importe toujours
   tous les routers dans le process principal. Les conditions de CLAUDE.md §7 ne
   sont pas remplies. Écrit explicitement dans l'entrée CHANGELOG du lot pour que
   le message de commit ne soit pas lu comme une annonce d'isolation.

4. **`npm ci` signale 6 vulnérabilités (2 moderate, 4 high)** dans le clone froid.
   Non corrigé (hors périmètre), non analysé.

5. **Warnings de build, non corrigés** (identiques en local et depuis le clone) :
   `Some chunks are larger than 500 kB after minification` — `index` 831,32 kB et
   `RichMessage` 556,79 kB.

6. **Bruit d'affichage, pas un échec** : PowerShell 5.1 enrobe la stderr des
   exécutables natifs en `NativeCommandError`. Les blocs `node.exe : [plugin
   builtin:vite-reporter]` dans les sorties de build en sont l'artefact ; le code
   de sortie des deux builds est **0**.

## Ce que je n'ai pas pu vérifier

- **La CI n'a pas tourné.** La note du modèle de rapport est à corriger sur un
  point : **`gh` est installé** (`C:\Program Files\GitHub CLI\gh.exe`) et
  authentifié (compte `ilyanndx-lab`, scopes `gist, read:org, repo, workflow`).
  Le seul blocage est que **la branche n'est pas poussée** : `hardening/v1` n'a
  pas d'upstream et `git ls-remote --heads origin hardening/v1` est vide, alors
  que le remote `origin` existe
  (`https://github.com/ilyanndx-lab/epure.git`). Je n'ai pas poussé : c'est une
  action sortante, hors du périmètre de la §2 et non demandée par la §4.
- **L'écart de version Python subsiste.** La §3.2 rejoue le *jeu de dépendances*
  du runner, pas sa version d'interpréteur : venv construit en **3.14.5**, alors
  que `ci.yml` fixe **3.12**. Une régression propre à 3.12 ne serait pas vue ici.
- **`npm run lint`** (eslint, `continue-on-error` en CI) n'est pas dans le script
  de vérification et n'a pas été lancé.
- **Les 2 tests skippés** des §3.1/§3.2 n'ont pas été identifiés individuellement.
- **Le job `integration`** (`integration_modules_mount.py`, torch + chromadb)
  n'est pas lancé par ce script.

## Sorties brutes

### §3.2 — `unittest discover` dans le venv CI isolé (queue de sortie)

```
core.codeagent.SecurityError: Identifiant de module invalide : '../chat'
ok
test_terminal_avec_id_invalide_ne_lance_aucun_process
(test_workshop_paths.WebSocketErrorTest.test_terminal_avec_id_invalide_ne_lance_aucun_process) ...
WARNING  core.module_workshop | SECURITY: identifiant de module refusé — '../chat'
ERROR    main | Ouverture terminal atelier
Traceback (most recent call last):
  File "C:\Users\Ilyan\epure\backend\main.py", line 583, in ws_workshop
  ...
  File "C:\Users\Ilyan\epure\backend\core\module_workshop.py", line 221, in _check_module_id
    raise SecurityError(f"Identifiant de module invalide : {module_id!r}")
core.codeagent.SecurityError: Identifiant de module invalide : '../chat'
ok
----------------------------------------------------------------------
Ran 144 tests in 15.464s
OK (skipped=2)
=== EXIT: 0 ===
```

(Les tracebacks ci-dessus sont le journal **attendu** des tests de refus de
sécurité — ils sont suivis de `ok`.)

Contre-épreuve §2.1, même venv, `uvicorn` désinstallé :

```
FAIL: test_worker_key_required (test_module_isolation.ModuleIsolationTest.test_worker_key_required)
AssertionError: False is not true : le worker hello n'a pas démarré
----------------------------------------------------------------------
Ran 5 tests in 7.758s
FAILED (failures=3)
=== EXIT: 1 ===
```

### §5.5 — `npm ci` puis `npm run build` depuis le clone froid

```
=== EXIT npm ci: 0 === 23.4203894 s
(6 vulnerabilities: 2 moderate, 4 high)

dist/assets/Component-Cr4UyCHv.js      19.83 kB │ gzip:   6.53 kB
dist/assets/ModuleBar-CFnW_rGi.js      20.76 kB │ gzip:   6.14 kB
dist/assets/Workshop-BTIphLME.js       26.76 kB │ gzip:   7.69 kB
dist/assets/Component-Bc8Go8NM.js      30.42 kB │ gzip:   7.75 kB
dist/assets/Component-CK0s32sb.js      46.79 kB │ gzip:  13.13 kB
dist/assets/RichMessage-BGch81eN.js   556.79 kB │ gzip: 170.09 kB
dist/assets/index-pPTdSnMg.js         831.32 kB │ gzip: 222.40 kB

✓ built in 2.61s
(!) Some chunks are larger than 500 kB after minification.
=== EXIT npm run build: 0 === duree: 13.173508 s
```

### §5.3 — `git status --porcelain` dans le clone froid

```
(aucune sortie — arbre propre immédiatement après clone)
```

### §5.1 / §5.2 — contenu du clone froid

```
--- backend\modules ---
admin  chat  code  docs  flashcards  hello
history  kholle  rangement  reviseur  settings  _atelier

--- frontend\src\modules\generated ---
hello  rangement

--- taille .git ---
69,3 Mo
```
