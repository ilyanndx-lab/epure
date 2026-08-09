# Démontage — option D : redémarrer plutôt que démonter

**Contexte.** `docs/limite-demontage.md` établit qu'à partir de fastapi 0.137.0,
`_drop_module_routes` ne retire plus rien et la route d'un module supprimé
continue de répondre 200. Cinq options y sont listées ; ce document exécute
**l'option D — ne plus démonter à chaud, redémarrer le backend**.

**Ce document ne remplace pas `limite-demontage.md`.** Il en suppose la lecture :
la mesure, la bissection et les tentatives y sont, et n'ont pas à être répétées.

---

## §0 — Corriger le cadrage : ce n'est pas un correctif de bug

À dire d'abord, parce que la formulation inverse a circulé et qu'elle mène à
mal prioriser.

**Sur la version épinglée (`fastapi==0.136.3`), `_drop_module_routes`
fonctionne.** La bissection de `limite-demontage.md` §3 le montre ligne 1 :
`5 → 4`, `GET /hello/ping` → `404`. Il n'y a **aucune route fantôme
aujourd'hui**, quelle que soit l'intensité d'usage du catalogue.

Ce que l'option D apporte n'est donc pas une correction, c'est un
**dépinglage** :

| | Aujourd'hui | Après option D |
|---|---|---|
| Routes fantômes | aucune (version épinglée) | aucune (plus de démontage du tout) |
| `fastapi` | gelé en 0.136.3 | librement montable |
| Dépendance aux internes | `app.router.routes`, `endpoint.__module__` | aucune |

La valeur est **le correctif de sécurité amont qu'on ne peut pas prendre tant
qu'on est gelé**. C'est un coût qui croît avec le temps, pas une panne actuelle.
À prioriser comme tel : important, pas urgent.

Corollaire : ce chantier n'est **pas** un prérequis du registre de modules.
Ils sont indépendants.

---

## §1 — Ce que le tray sait déjà faire, et ce qui manque

Lecture de `epure_tray.py` au 2026-08-09.

**Ce qui existe** : `_do_restart()` = `_stop_processes()` + `sleep(2)` +
`_start_processes()`, câblé sur l'entrée de menu « Redémarrer ».

**Pourquoi ce n'est pas utilisable tel quel** :

| Effet de `_do_restart()` | Conséquence pour une désinstallation de module |
|---|---|
| `_kill_existing()` fait `taskkill /F /IM ollama.exe` | Ollama meurt. Avec `OLLAMA_KEEP_ALIVE=-1`, le modèle est en VRAM : il faut le recharger |
| `flm.exe` tué aussi | idem côté NPU |
| `npm run dev` relancé | Vite redémarre, l'onglet perd sa connexion |
| `sleep(4)` + `sleep(6)` + `sleep(8)` | ~18 s, et un onglet de navigateur s'ouvre en plus (`webbrowser.open`) |

Redémarrer toute la pile pour retirer un module est disproportionné. Il faut un
**redémarrage du backend seul**.

**Ce qui manque vraiment** : le backend n'a **aucun canal vers le tray**. Ce
sont deux processus, le tray est le parent, et rien ne remonte. C'est le seul
morceau réellement nouveau de ce chantier.

---

## §2 — Le canal : fichier sentinelle

Trois options ont été pesées ; une seule tient sous Windows.

| Option | Verdict |
|---|---|
| `os.execv` depuis le backend (le processus se remplace lui-même) | **Écartée.** Sous Windows, `execv` ne remplace pas le processus : il en crée un nouveau et termine l'ancien. Le `Popen` du tray verrait donc uvicorn mourir et le compterait comme un crash. Comportement correct sous Linux, faux sur la plateforme primaire — exactement le type de choix que CLAUDE.md §1 interdit. |
| Le tray expose un petit serveur HTTP local | **Écartée.** Nouveau socket en écoute, donc nouvelle surface d'authentification à concevoir et à défendre, pour déclencher un `Popen`. Le rapport valeur/risque est mauvais. |
| **Fichier sentinelle surveillé par le tray** | **Retenue.** Aucun port, aucune authentification nouvelle, aucun interne d'OS. Le backend écrit, le tray voit, le tray redémarre uvicorn seul. |

```
backend écrit  memory/.restart-requested   (contenu : la raison, pour le journal)
        │
        ▼
thread du tray, sondage 2 s
        │
        ▼
efface la sentinelle, terminate() du seul p_uvicorn, relance uvicorn seul
```

Points de conception :

- **La sentinelle est effacée par le tray avant le redémarrage**, pas après :
  sinon un backend qui redémarre et relit un fichier encore présent entre en
  boucle.
- **Seul `p_uvicorn` est arrêté.** Ni Ollama, ni `flm`, ni Vite. Extraire de
  `_start_processes()` une fonction `_start_uvicorn()` que les deux chemins
  appellent — pas une seconde construction de la ligne de commande, qui
  divergerait de la première (`--no-access-log`, `EPURE_RELOAD`, `_bind_host()`
  et leurs justifications sont dans le fichier).
- **La sentinelle vit dans `resolve_data_dir()`**, pas dans un chemin en dur
  (CLAUDE.md §3.5), et s'écrit par `core/jsonstore` — c'est un fichier de
  runtime comme les autres.

### Quand il n'y a pas de tray

Backend lancé à la main (`python -m uvicorn main:app --reload`) ou en Docker :
personne ne lit la sentinelle. L'endpoint doit **le savoir et le dire**, pas
promettre un redémarrage qui n'arrivera jamais.

Détection explicite : le tray pose `EPURE_TRAY=1` dans l'environnement d'uvicorn
qu'il lance. Absent ⇒ la réponse de désinstallation porte
`redémarrage_requis: true, automatique: false`, et l'UI affiche « redémarrez
Épure pour terminer la suppression ». Deviner par heuristique (regarder le
processus parent) serait fragile ; une variable posée par celui qui sait est
franche.

---

## §3 — Étapes

### Étape A — Redémarrage du backend seul, sans rien y brancher

`epure_tray.py` : extraire `_start_uvicorn()`, ajouter le thread de surveillance
et `_restart_backend()`. Poser `EPURE_TRAY=1`.

Vérification manuelle : toucher la sentinelle à la main pendant qu'Ollama tourne
avec un modèle chargé → uvicorn redémarre, `ollama ps` montre le modèle
**toujours chargé**, l'onglet du navigateur n'est pas remplacé.

Commit : `feat(tray): redémarrage du backend seul sur sentinelle`

> **Constat annexe pendant la lecture, à traiter séparément.**
> `epure_tray.py` lance `npm run dev` avec `shell=True`, alors que CLAUDE.md §6
> pose « IMPÉRATIF : aucun `shell=True` ». Les arguments sont constants, donc
> rien n'est injectable et le risque réel est nul — mais c'est une violation
> d'un invariant écrit, dans un fichier que cette étape modifie de toute façon.
> Probable cause : sous Windows `npm` est un `.cmd`, que `Popen` ne résout pas
> sans shell. Correctif attendu : `npm.cmd`, ou `shutil.which("npm")`.
> **Commit séparé** — ne pas mélanger un correctif d'invariant avec un
> changement de comportement.

### Étape B — La désinstallation demande le redémarrage

`core/catalogue.py:uninstall()` : remplacer l'appel à `_drop_module_routes(app, mid)`
par l'écriture de la sentinelle.

L'ordre du reste **ne change pas** : sauvegarde d'abord, suppression ensuite. Et
un commentaire du code actuel devient caduc :

```python
# Les routes d'abord : une fois les fichiers effacés, plus rien ne permet de
# retrouver le nom de module d'un endpoint pour filtrer.
```

Cette contrainte disparaît avec le filtrage. La retirer, pas la laisser
mentir — c'est la convention de docstring du dépôt.

La purge de `sys.modules` et `importlib.invalidate_caches()`, elles, **restent** :
elles sont justifiées indépendamment (docstring de `uninstall()`), et un
redémarrage les rend simplement redondantes plutôt qu'inutiles.

`DELETE /settings/modules/{id}` retourne `redémarrage_requis` et `automatique`.

Commit : `fix(catalogue): la désinstallation redémarre le backend au lieu de démonter`

### Étape C — Le frontend attend proprement

Après une désinstallation : état d'attente, sondage de `/health`, reprise quand
il répond. **Ce qui casse et qu'il faut assumer** : les flux SSE et WebSocket en
cours sont coupés. Une génération de chat en cours meurt.

Deux garde-fous, dans cet ordre de préférence :

1. avertir avant (« une génération est en cours ») si l'information est
   disponible côté frontend ;
2. à défaut, ne pas laisser l'UI se figer : l'attente doit être visible et
   bornée, avec un message clair si `/health` ne revient pas.

Commit : `feat(settings): attente de redémarrage après désinstallation`

### Étape D — Retirer la garde de version

Une fois A-C verts :

- supprimer `_drop_module_routes` de `core/module_workshop.py` et ses appels ;
- `requirements.txt` : lever `fastapi==0.136.3` et `starlette==1.2.0` vers des
  bornes larges ;
- `ci.yml` : idem ;
- `backend/test_versions_epinglees.py` : supprimer la garde sur
  `fastapi.__version__`, **garder** la vérification que `ci.yml` et
  `requirements.txt` s'accordent — celle-là reste vraie et utile ;
- `docs/limite-demontage.md` : en-tête mis à jour, « bug ouvert » → « contourné
  par l'option D, cf. `docs/demontage-option-d.md` ». Ne pas supprimer le
  document : la mesure garde sa valeur, et l'option B y reste consignée si
  quelqu'un veut du démontage à chaud un jour.

**Les deux tests de route fantôme doivent être réécrits, pas supprimés.** Ils
définissent ce qui doit rester vrai (« un module supprimé ne répond plus »).
Avec l'option D, ce n'est plus vérifiable dans le processus courant. Ils
deviennent :

- un test unitaire : la désinstallation **écrit la sentinelle** ;
- un test d'intégration : après désinstallation **et redémarrage**, la route
  répond 404. Il exige un vrai processus, donc `integration_*.py` (job manuel),
  pas `test_*.py`.

Cette dégradation est réelle : on échange une vérification en processus contre
une vérification qui coûte un processus. C'est le prix de l'option D, et il faut
l'écrire dans le CHANGELOG plutôt que de laisser croire à un gain net.

Commit : `chore(deps): dépinglage de fastapi après suppression du démontage à chaud`

---

## §4 — Ce que l'option D ne change pas

- **Le montage à chaud continue de fonctionner.** `include_router` marche sur
  toutes les versions ; seul le *démontage* était cassé. `catalogue.install()`
  garde son `_remount` et l'installation reste instantanée. **L'asymétrie est
  voulue : installer est fréquent et rapide, désinstaller est rare et coûte un
  redémarrage.**
- **La réapprobation d'un module dans l'Atelier**, en revanche, passe par le même
  `_remount`, donc par le même démontage. Elle doit vraisemblablement emprunter
  le chemin sentinelle elle aussi. **Vraisemblablement** : `limite-demontage.md`
  §9 signale que ce chemin n'a jamais été exercé en ≥ 0.137 et que c'est une
  déduction, pas une mesure. **À mesurer avant l'étape D**, pas à supposer — si
  la réapprobation reste cassée après dépinglage, le dépinglage est prématuré.

---

## §5 — Ce qui n'a pas été vérifié

- `core/module_workshop.py` (78 Ko) n'a pas été lu. `_drop_module_routes` est
  connu par sa citation dans `limite-demontage.md` §1 et par son usage dans
  `catalogue.py`. La liste exacte de ses appelants **n'a pas été établie** —
  à faire avant l'étape D, un appelant oublié laisserait un chemin mort.
- Le comportement de `os.execv` sous Windows est cité de mémoire, pas mesuré sur
  ce poste. Il motive une option écartée, pas l'option retenue — mais si
  quelqu'un veut réhabiliter `execv`, qu'il le mesure d'abord.
- Le délai réel d'un redémarrage d'uvicorn seul n'est pas mesuré. Les `sleep()`
  actuels du tray (4 / 6 / 8 s) sont dimensionnés pour un démarrage complet et
  n'ont pas de raison d'être repris tels quels pour un redémarrage partiel.
- Aucune mesure de ce que coûte, côté frontend, une coupure SSE en pleine
  génération.

---

## §6 — Prompts pour Claude Code

### Étape A

> Lis `docs/demontage-option-d.md` §1 à §3 étape A. Dans `epure_tray.py` :
> extrais de `_start_processes()` une fonction `_start_uvicorn()` — **ne
> reconstruis pas la ligne de commande**, les choix qui y sont commentés
> (`--no-access-log`, `EPURE_RELOAD`, `_bind_host()`) doivent rester à un seul
> endroit. Ajoute un thread démon qui sonde toutes les 2 s un fichier sentinelle
> sous `resolve_data_dir()` : s'il existe, il l'efface **puis** arrête et relance
> **le seul** processus uvicorn. Ni Ollama, ni `flm`, ni Vite ne doivent être
> touchés. Pose `EPURE_TRAY=1` dans l'environnement d'uvicorn.
>
> Vérifie à la main : avec un modèle chargé en VRAM, toucher la sentinelle
> redémarre uvicorn et `ollama ps` montre le modèle toujours chargé, sans nouvel
> onglet de navigateur.
> Commit : `feat(tray): redémarrage du backend seul sur sentinelle`.
>
> Dans un **commit séparé** : `epure_tray.py` lance `npm run dev` avec
> `shell=True`, ce que CLAUDE.md §6 interdit. Les arguments sont constants donc
> rien n'est injectable, mais l'invariant est écrit. Corrige (`npm.cmd` ou
> `shutil.which`) et vérifie que le frontend démarre toujours sous Windows.

### Étapes B et C

> Étape B. Dans `core/catalogue.py:uninstall()`, remplace
> `_drop_module_routes(app, mid)` par l'écriture de la sentinelle. Garde l'ordre
> sauvegarde → suppression, garde la purge de `sys.modules` et
> `importlib.invalidate_caches()`. Retire le commentaire « Les routes d'abord :
> une fois les fichiers effacés… » : sa contrainte n'existe plus, et un
> commentaire faux est pire qu'absent. `DELETE /settings/modules/{id}` retourne
> `redémarrage_requis` et `automatique` (faux si `EPURE_TRAY` est absent — dans
> ce cas l'UI dit à l'utilisateur de redémarrer lui-même, elle ne promet rien).
> Commit : `fix(catalogue): la désinstallation redémarre le backend au lieu de démonter`.
>
> Étape C. Côté frontend, état d'attente après désinstallation, sondage de
> `/health`, message clair si le backend ne revient pas. Les flux SSE en cours
> sont coupés : l'UI ne doit pas rester figée dessus.
> Commit : `feat(settings): attente de redémarrage après désinstallation`.

### Étape D

> Étape D, **seulement si A-C sont verts**. Avant tout : mesure ce que fait la
> **réapprobation** d'un module dans l'Atelier en fastapi ≥ 0.137 — approuve deux
> fois de suite le même id et regarde quelle version est servie.
> `docs/limite-demontage.md` §9 dit que ce chemin n'a jamais été exercé et que
> l'échec est déduit, pas mesuré. S'il est cassé, il doit emprunter la sentinelle
> lui aussi **avant** tout dépinglage.
>
> Établis ensuite la liste complète des appelants de `_drop_module_storage` et
> `_drop_module_routes` dans `core/module_workshop.py`, supprime la fonction et
> ses appels, lève les épingles de `requirements.txt` et `ci.yml`, retire de
> `test_versions_epinglees.py` la garde sur `fastapi.__version__` mais **garde**
> la vérification de cohérence entre `ci.yml` et `requirements.txt`.
>
> Réécris les deux tests de route fantôme, ne les supprime pas : un test unitaire
> qui vérifie que la sentinelle est écrite, et un `integration_*.py` qui vérifie
> le 404 après vrai redémarrage. Mets à jour l'en-tête de
> `docs/limite-demontage.md` et écris dans le CHANGELOG ce que cet échange coûte
> — une vérification en processus troquée contre une vérification qui exige un
> processus.
> Commit : `chore(deps): dépinglage de fastapi après suppression du démontage à chaud`.
