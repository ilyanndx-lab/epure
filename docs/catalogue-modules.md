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

## §1 — Deux états d'un module, une seule source de vérité

**À faire avant d'ajouter quoi que ce soit**, sinon la dette est immédiate.

### Décision : deux états, pas trois

| État | Source de vérité | Effet | Stockage |
|---|---|---|---|
| **Installé** | présence de `backend/modules/<id>/manifest.json` | le module existe pour cette instance | aucun — dérivé du disque |
| **Actif** | appartenance à `instance_config.modules_activés` | routeur monté **et** visible dans la barre, à la position donnée par la liste | `memory/instance_config.json` |

`modules_activés` est une **liste ordonnée** et devient l'**unique** source de
vérité. Il n'y a pas d'état « monté mais invisible » : un module actif est monté
et affiché, un module inactif n'est ni l'un ni l'autre.

### Pourquoi pas trois états

La version précédente de ce plan séparait « actif » (montage, via
`modules_state.json`) et « épinglé + ordre » (barre, via `modules_activés`).
Cette séparation est abandonnée pour deux raisons :

1. **Elle ne décrit aucun cas réel.** Il n'existe aujourd'hui aucun module
   monté-mais-invisible, ni aucun besoin identifié d'en avoir un. On paierait la
   complexité d'un état pour un cas hypothétique.
2. **Deux fichiers pour une notion produisent la divergence, mécaniquement.**
   Constat sur l'instance de référence avant migration :

   - `modules_state.json` : 11 entrées, dont **9 fantômes** (`minuteur`, `snake`,
     `emojis`, `pong`, `clicker`, `vroom`, `dinosaure`, `minecraft`, `astral`)
     pointant des modules supprimés à l'étape A ;
   - `modules_activés` : 12 entrées, dont **4 fantômes** (`snake`, `minecraft`,
     `dinosaure`, `astral`) ;
   - `reviseur` : installé, monté, fonctionnel — et **absent** de
     `modules_activés`. Cause : le défaut `_CORE_MODULES` de `core/instance.py`
     est une liste écrite en dur qui ne le mentionne pas (ni `settings`, ni
     `hello`). Un module ajouté au dépôt après cette constante n'entrait jamais
     dans la liste.

   Les deux fichiers ne divergeaient pas par accident : rien ne les tenait
   ensemble.

`backend/memory/modules_state.json` **disparaît**. Il ne doit pas être recréé —
cf. `CLAUDE.md` §3.3.

### Règles dérivées

- **Défaut sur une installation neuve** : `modules_activés` absent ou vide → tous
  les modules installés sont actifs, dans l'ordre de `discover_manifests()`
  (tri alphabétique du nom de dossier, donc déterministe et reproductible).
  C'est ce que voit quelqu'un qui clone le dépôt et démarre, sans rien régler.
  L'invariant tient parce que la liste ne peut pas devenir vide par l'usage :
  `settings` est indésactivable, donc « vide » signifie sans ambiguïté « jamais
  initialisée », jamais « tout désactivé par l'utilisateur ».
- **`settings` reste indésactivable** — sinon plus d'écran pour réactiver quoi
  que ce soit. Refusé par `set_status`, et réinjecté à la lecture si la liste
  stockée l'a perdu.
- **Un id présent mais non installé est purgé** au démarrage (entrée fantôme).
- **Un module installé absent de la liste est ajouté en fin** : l'absence
  signifie « jamais vu », pas « masqué ». C'est ce qui rattrape `reviseur`, et ce
  qui fera entrer tout module installé depuis le catalogue à l'étape C.

### Migration, une seule fois au démarrage

Journalisée ligne par ligne, dans cet ordre :

| # | Opération | Sur l'instance de référence |
|---|---|---|
| a | tout module `status: "disabled"` d'un ancien `modules_state.json` est retiré de `modules_activés` **et exclu de l'étape c** | aucun (les 11 entrées sont `active`) |
| b | tout id de `modules_activés` non installé est purgé | `snake`, `minecraft`, `dinosaure`, `astral` |
| c | tout module installé absent de `modules_activés` est ajouté en fin | `hello`, `reviseur`, `settings` |
| d | `modules_state.json` est supprimé | 11 entrées jetées |

L'exclusion en (a) est ce qui rend (a) et (c) compatibles au sein d'un même
passage : sans elle, un module explicitement désactivé serait retiré par (a) puis
aussitôt réajouté par (c). « Désactivé » est une information, « absent » n'en est
pas une.

#### Correction apportée à l'implémentation : (a) et (c) sont conditionnelles

Le plan ci-dessus, appliqué tel quel à **chaque** démarrage, ne tient pas. Le
test d'idempotence l'a montré :

> (a) exclut `zeta` parce que `modules_state.json` le dit désactivé. (d) supprime
> ce fichier. Au démarrage suivant, plus rien ne porte l'information : (c) voit
> `zeta` installé et absent de la liste, et le réintègre.

Le défaut ne se limite pas à la migration. Une fois la bascule faite, `set_status`
désactive un module **en le retirant de la liste** — c'est tout le modèle à deux
états. Donc « installé et absent » ne signifie plus « jamais vu » mais
« désactivé par l'utilisateur », et (c) à chaque démarrage **annulerait toute
désactivation au reboot suivant**.

Règle retenue :

| Étape | Quand |
|---|---|
| (a) désactivations héritées | **bascule seulement** — `modules_state.json` présent, ou liste vide |
| (b) purge des fantômes | **à chaque démarrage** (un module effacé du disque doit sortir de la barre) |
| (c) installés absents ajoutés | **bascule seulement** — même condition que (a) |
| (d) suppression de l'ancien fichier | **bascule seulement**, par construction |

Un module installé après coup n'a pas besoin de (c) : l'installation depuis le
catalogue (étape C) passera par `set_status(id, "active")`, qui l'ajoute.

La migration est ainsi **idempotente** : en régime établi, (b) ne trouve rien et
il n'y a aucune écriture. C'est testé, ainsi que le cas d'usage réel — désactiver
un module puis redémarrer doit le laisser désactivé.

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
