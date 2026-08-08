# Cœur minimal + catalogue de modules — plan d'exécution

**Contexte.** Épure va être partagé à quelques proches qui l'installeront chez
eux et le personnaliseront. Le dépôt doit donc contenir un **cœur** utilisable
seul, plus un **catalogue** de modules installables à la demande. Les modules
générés par l'Atelier restent personnels et hors dépôt.

**Prérequis.** Le durcissement v1 (5 lots) est committé sur `hardening/v1`. Ce
plan démarre après, sur une base dont les chemins et les entrées client sont
déjà confinés — ce qui compte, parce qu'on va ajouter un endpoint destructif.

**Décisions actées avec Ilyann :**

| Question | Décision |
|---|---|
| Cœur | `admin`, `chat`, `history`, `settings` (+ `hello`, référence de l'Atelier) |
| Installables | `code`, `docs`, `flashcards`, `kholle`, `reviseur`, `rangement` |
| Générés jetables | `astral`, `clicker`, `dinosaure`, `emojis`, `minecraft`, `minuteur`, `pong`, `snake`, `vroom` |
| Distribution | Catalogue **local** dans le dépôt, pas de téléchargement réseau (cf. §0) |
| Dépôt | Privé, invitations nominatives, tant que la purge ONNX + LICENSE ne sont pas faites |

---

## §0 — Pourquoi un catalogue local et pas un vrai téléchargement

`frontend/src/modules/registry.ts:45-55` déclare les composants cœur en imports
statiques (`lazy(() => import('./kholle/Component'))`), résolus au build. Trois
options ont été pesées :

| Option | Verdict |
|---|---|
| Le code reste dans le bundle, « installer » = activer | Écartée : ne répond pas à « pas de base », le code est chez tout le monde. |
| Téléchargement de JS distant à l'exécution (module federation) | **Écartée.** Exécuter du JS tiers dans l'origine de l'app annule la frontière qu'on vient de poser : tout module chargé peut lire `localStorage['epure.apiToken']`. À reconsidérer seulement après l'isolation worker. |
| **Catalogue local : copie de fichiers depuis `modules-catalogue/`** | **Retenue.** Aucun réseau, aucune nouvelle frontière de confiance, et c'est exactement le mécanisme de `module_workshop.approve()` (copie staging → `modules/` + `generated/`) : on réutilise du code éprouvé au lieu d'en inventer. |

Limite assumée : l'installation modifie `frontend/src/`, donc elle suppose le
serveur de dev (`npm run dev`, ce que lance `epure_tray.py`). Avec un frontend
déjà buildé (image Docker), un module installé n'apparaît qu'au rebuild. À
documenter dans le README, pas à contourner.

---

## §1 — Clarifier les trois états d'un module

**À faire avant d'ajouter quoi que ce soit**, sinon la dette est immédiate.

Aujourd'hui, deux notions coexistent déjà :

- `backend/memory/modules_state.json` → `status: active | disabled`
  (`core/module_registry.py:111`, `set_status`) — conditionne le **montage du
  routeur**.
- `instance_config.modules_activés` (liste ordonnée) — conditionne
  **l'affichage dans la barre** et son ordre
  (`frontend/src/modules/settings/Component.tsx:296-312`).

Le catalogue en ajoute un troisième : **présent sur disque ou non**.

Modèle cible, à écrire noir sur blanc dans `CLAUDE.md` :

| État | Source de vérité | Effet |
|---|---|---|
| **Installé** | présence de `backend/modules/<id>/manifest.json` | le module existe pour cette instance |
| **Actif** | `modules_state.json.status` | son routeur est monté, son composant résolvable |
| **Épinglé + ordre** | `instance_config.modules_activés` | présence et position dans la barre latérale |

Règle : `modules_activés` ne doit plus servir à activer/désactiver — seulement à
ordonner ce qui est déjà actif. Un id qui y figure sans être installé est purgé
au démarrage (aujourd'hui il produit une entrée fantôme).

---

## §2 — Étapes

### Étape A — Sortir les modules générés jetables

Neuf modules (`astral`, `clicker`, `dinosaure`, `emojis`, `minecraft`,
`minuteur`, `pong`, `snake`, `vroom`) : `git rm -r --cached` sur
`backend/modules/<id>/` et `frontend/src/modules/generated/<id>/`, **puis
suppression du disque** (contrairement au plan précédent : ce sont des tests,
Ilyann n'en veut plus localement non plus).

Ils restent récupérables dans l'historique :
`git show HEAD:backend/modules/astral/router.py`. Aucune purge n'est prévue sur
eux, donc rien n'est perdu.

`rangement` **ne suit pas ce chemin** : il part au catalogue (étape C).

Gain immédiat : 3 des 17 erreurs de build viennent de `clicker`, `minuteur` et
`vroom`. Elles disparaissent sans écrire une ligne — et légitimement, ce sont
des artefacts personnels, pas du code produit.

`backend/modules/_atelier/MODULE_INDEX.md` liste les 10 modules générés et sert
de contexte au LLM (« Modules existants dans cette instance »). Le réduire au
cœur + hello, et le faire régénérer par l'Atelier plutôt que le maintenir à la
main.

`.gitignore` — allowlist plutôt que split de dossiers, pour ne pas refactorer
deux fois les mêmes chemins (`MODULES_DIR` de `module_workshop.py`, 76 Ko, sera
touché par le chantier d'isolation) :

```gitignore
# Modules générés par l'Atelier : personnels, hors dépôt.
# Cœur + catalogue listés en exception — une ligne par nouveau module suivi.
backend/modules/*/
!backend/modules/_atelier/
!backend/modules/admin/
!backend/modules/chat/
!backend/modules/hello/
!backend/modules/history/
!backend/modules/settings/

frontend/src/modules/generated/*/
!frontend/src/modules/generated/hello/
```

Commit : `chore(modules): sort les modules générés de test du dépôt`

### Étape B — Rendre la CI verte

Il reste les erreurs de `frontend/src/components/Workshop.tsx` et voisines.
`npm run build` est bloquant depuis le lot 5 : tant que ce n'est pas fait,
chaque push est rouge et le gate perd sa crédibilité.

Point de stabilité : **ne pas entamer l'étape C avant que la CI soit verte.**
Sinon deux chantiers cassent en même temps et on ne sait plus lequel.

Commit : `fix(frontend): erreurs de type bloquant le build`

### Étape C — Le catalogue

Structure :

```
modules-catalogue/
  code/        manifest.json  router.py  Component.tsx
  docs/        …
  flashcards/  …
  kholle/      …
  reviseur/    …
  rangement/   …
```

Déplacements :

- `backend/modules/<id>/` → `modules-catalogue/<id>/` pour les six.
- `frontend/src/modules/<id>/Component.tsx` → `modules-catalogue/<id>/Component.tsx`
  (`rangement` vient de `frontend/src/modules/generated/rangement/`).
- Retirer les six de `CORE_DEFS` (`registry.ts:45-55`). Une fois installés, ils
  sont découverts par le glob `./generated/**/*.tsx` comme n'importe quel module
  — donc **installer copie le composant dans `generated/<id>/`**, pas dans
  `modules/<id>/`.
- Les six manifestes passent à `core_module: false`, `origin: "catalogue"`,
  `removable: true`. `origin` distinct de `"workshop"` : ce n'est pas du code
  généré, il ne doit pas être proposé à la ré-édition par défaut.

Backend — trois endpoints, dans le routeur `settings` :

| Route | Contrat | Contrôles |
|---|---|---|
| `GET /settings/catalogue` | liste des manifestes de `modules-catalogue/`, chacun avec `installé: bool` | lecture seule |
| `POST /settings/catalogue/{id}/install` | copie catalogue → `modules/<id>/` + `generated/<id>/`, puis `set_status(id,"active")` et montage | `_ID_RE`, id ∈ catalogue, refus si déjà installé |
| `DELETE /settings/modules/{id}` | sauvegarde dans `_backups/<id>/<horodatage>/` puis suppression des deux dossiers | `_ID_RE`, **refus si `core_module` ou `removable: false`**, refus si id inconnu |

⚠️ `DELETE` est un endpoint destructif qui prend un identifiant venant du
client — exactement la classe de faille fermée au lot 3 (`_staging_dir`
acceptait `../chat`). Il **doit** passer par `_ID_RE` puis
`_modules_safe_path`, et sauvegarder avant de supprimer. Un test dédié est
obligatoire, pas optionnel.

Réutiliser la copie de `module_workshop.approve()` plutôt que d'en écrire une
seconde : c'est le même geste (valider, sauvegarder, copier, monter).

Commits : `feat(catalogue): modules installables à la demande` puis
`feat(settings): installation et suppression de modules`

### Étape D — Le panneau « santé des modules »

Dans Réglages, à côté du catalogue. Une ligne par module installé :

| Colonne | Source |
|---|---|
| Module | manifeste |
| État | monté / non monté (`module_registry.list_modules` + ce qui est réellement dans `app.routes`) |
| Routeur | `module_validate.validate_router_py` sur le `router.py` sur disque |
| Composant | `tsc --noEmit` sur le `Component.tsx` |
| Actions | **Supprimer** (`DELETE`, désactivé pour le cœur) · **Corriger** (ouvre l'Atelier en édition sur cet id) |

Backend : `GET /settings/modules/diagnostic`. Les deux validations existent
déjà (`core/module_validate.py`) — il s'agit de les exposer en lot, pas d'en
écrire de nouvelles.

Coût réel : `tsc` sur chaque module prend plusieurs secondes. Donc **diagnostic
à la demande** (un bouton « Analyser »), pas au chargement de la page, et
résultat mis en cache — le même piège que `engines_status`, déjà résolu par un
cache dans `module_workshop.py` (commit `e498e65`, 13,6 s → caché). Reprendre ce
pattern.

« Corriger » ne réimplémente rien : ça navigue vers l'Atelier avec l'id
pré-rempli, le flux d'édition existe (`POST /workshop/{id}/edit`).

Commit : `feat(settings): panneau de santé des modules (diagnostic, supprimer, corriger)`

### Étape E — Lot 6 du durcissement : `tsc` bloquant à l'approbation

Reporté depuis le durcissement, et **il devient obligatoire ici**. Une fois les
modules générés hors du dépôt, plus rien en CI ne vérifie le TSX produit par
l'Atelier — la CI ne voit que ce qui est commité. Le gate d'approbation devient
le seul point de contrôle.

`core/module_validate.py:392` : faire dépendre `report.ok` du résultat de `tsc`
au lieu de remonter un warning. Garder le comportement best-effort si
`tsc`/`npx` est introuvable (échec d'outillage ≠ échec de validation).
`force=true` sur `/workshop/{id}/approve` reste l'échappatoire.

Commit : `fix(atelier): tsc bloquant à l'approbation d'un module`

---

## §3 — Avant d'inviter qui que ce soit

Indépendant des étapes ci-dessus, mais bloquant pour le partage :

- **`LICENSE` absent.** Sans licence, le droit d'auteur par défaut s'applique :
  personne n'a le droit de modifier le code — contradictoire avec « qu'ils
  puissent customiser ». MIT pour un maximum de liberté, AGPL pour que les
  dérivés restent ouverts. (Je ne suis pas juriste ; l'absence de fichier est un
  fait, le choix de licence t'appartient.)
- **`piper_models/fr_FR-upmc-medium.onnx` : 76 Mo dans l'historique**, `.git`
  pèse 70 Mo. Chaque clone les paie. Le télécharger au premier démarrage et
  purger l'historique — **à faire avant qu'ils clonent**, une réécriture
  d'historique après coup les oblige à re-cloner.
- **`.claude/settings.local.json` versionné** : expose l'arborescence
  `C:\Users\Ilyan\**` et 120 règles de permission dont `Bash(python -)`.
  `git rm --cached` + `.gitignore`.
- **`litellm.yaml:7` : `master_key: sk-epure-local`** en dur. Variable
  d'environnement. Supprimer aussi `litellm.yaml.txt` (doublon).
- **`start.ps1:6` : `cd C:\Users\Ilyan\epure\backend`** — ne marche chez
  personne d'autre. Chemin relatif au script.
- **README vérifié sur une machine vierge.** Le seul test qui compte pour un
  onboarding : suivre son propre README depuis un clone frais, sans rien
  supposer d'installé.
- **`CLAUDE.md`, `docs/`, `backend/core/module_worker.py`,
  `backend/test_module_isolation.py`** sont encore untracked. Le CHANGELOG note
  lui-même que le test d'isolation n'est pas vu par la CI faute d'être commité.

---

## §4 — Prompts pour Claude Code

### Étape A

> Lis `docs/catalogue-modules.md`, étape A. Sors du dépôt **et du disque** les
> neuf modules générés de test : astral, clicker, dinosaure, emojis, minecraft,
> minuteur, pong, snake, vroom — `backend/modules/<id>/` et
> `frontend/src/modules/generated/<id>/`. **Ne touche pas à `rangement`** (il
> part au catalogue) ni à `hello` (référence de l'Atelier, reste versionné des
> deux côtés). Applique le `.gitignore` du document. Réduis
> `backend/modules/_atelier/MODULE_INDEX.md` au cœur + hello. Vérifie ensuite
> dans un clone frais temporaire que `discover_manifests()` ne renvoie que le
> cœur + hello + rangement, et que `import.meta.glob` sur `generated/` ne
> résout que hello et rangement. Commit :
> `chore(modules): sort les modules générés de test du dépôt`.

### Étape B

> Étape B. Corrige les erreurs restantes de `npm run build`. Montre-moi le
> diff de `frontend/src/components/Workshop.tsx` **avant** de committer : 50 Ko
> de composant, c'est là que le risque de régression silencieuse est le plus
> élevé. Ne corrige que ce qui bloque le build ; pas de refactor opportuniste.
> `npm run build` doit passer, et `python -m unittest discover -s . -p 'test_*.py'`
> aussi. Commit : `fix(frontend): erreurs de type bloquant le build`.

### Étape C

> Lis `docs/catalogue-modules.md`, §1 et étape C. Commence par §1 : clarifie les
> trois états (installé / actif / épinglé), documente-les dans `CLAUDE.md`, et
> fais en sorte qu'un id présent dans `modules_activés` mais non installé soit
> purgé au démarrage. Puis l'étape C. Réutilise la logique de copie de
> `module_workshop.approve()`, n'en écris pas une seconde.
> `DELETE /settings/modules/{id}` est destructif et prend un id client :
> `_ID_RE` puis `_modules_safe_path`, refus si `core_module` ou
> `removable: false`, sauvegarde dans `_backups/` avant suppression, et un
> fichier de test dédié couvrant `../chat`, un id core, un id inconnu. Deux
> commits séparés : le catalogue backend, puis l'UI Réglages.

### Étape D

> Étape D. `GET /settings/modules/diagnostic` + le panneau dans Réglages.
> Diagnostic **à la demande** avec cache, sur le modèle du cache
> d'`engines_status` dans `module_workshop.py` (commit e498e65) — pas
> d'analyse au chargement de la page. « Corriger » navigue vers l'Atelier avec
> l'id pré-rempli, ne réimplémente pas l'édition. Commit :
> `feat(settings): panneau de santé des modules`.

### Étape E

> Étape E, reprise du lot 6 du durcissement. `core/module_validate.py:392` :
> `report.ok` dépend du résultat de `tsc` au lieu d'un warning. Best-effort
> conservé si `tsc`/`npx` est introuvable. Vérifie qu'un `Component.tsx` avec
> une erreur de type fait bien échouer `POST /workshop/{id}/approve`, et
> qu'avec `force=true` il passe quand même. Commit :
> `fix(atelier): tsc bloquant à l'approbation d'un module`.
