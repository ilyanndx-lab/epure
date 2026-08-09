# Changelog

## 2026-08-09 — fastapi/starlette épinglés en CI ; le démontage de routes reste cassé au-delà de 0.136

La CI installait `fastapi` sans épingle : pip résolvait donc la dernière version
publiée à chaque push, pendant que le poste de dev restait en 0.136.3, celle de
`requirements.txt`. C'est la version validée qui fait foi — l'inverse n'est pas
un choix, c'est un bump silencieux. `ci.yml` installe désormais
`fastapi==0.136.3 starlette==1.2.0`, et son en-tête ne présente plus la liste
non épinglée comme délibérée. `starlette` entre dans `requirements.txt` bien
qu'il arrive en transitif : fastapi le déclare `>=0.46.0` sans borne haute, donc
sa version était subie et non choisie, alors qu'on dépend de ses internes.

### Ce que l'épinglage cache, et qu'il faut lire avant de le lever

**Le bug de `_remount` / `_drop_module_routes` sur fastapi ≥ 0.137 est antérieur
au catalogue, et il n'est PAS corrigé.** Dit explicitement parce que la
prochaine personne à monter la version verra deux tests du catalogue devenir
rouges et cherchera la cause dans le catalogue : elle n'y est pas.
`_drop_module_routes` vit dans `core/module_workshop.py`, appelé par `_remount`,
et était déjà cassé avant que le catalogue existe. Le catalogue n'a fait que
l'exposer, en ajoutant les deux premiers tests qui suppriment un module puis
vérifient que son API se taise.

Ce qui se passe : à partir de **fastapi 0.137.0** (bascule mesurée par
bissection — pas 0.141, comme on l'avait d'abord cru), `include_router`
n'aplatit plus les routes dans `app.router.routes`. Il y met une seule entrée
`_IncludedRouter`, sans `endpoint`, derrière laquelle vivent les vraies routes,
servies via un cache invalidé par un compteur de version. Le filtre de
`_drop_module_routes` ne trouve alors plus rien à retirer — la liste passe de 5
à 5 — et la route d'un module supprimé **continue de répondre 200**. Une API
fantôme, pas une panne visible.

La cause est côté **fastapi**, pas Starlette, contrairement à ce que suggérait
le message d'échec d'origine : 0.136.3 + starlette 1.6.0 passe les 207 tests.

Ce n'était pas théorique — la CI était rouge sur les deux derniers commits
(`8fdc31c`, run `31277621748`), avec `fastapi-0.141.1` résolu par pip. À noter
que `8fdc31c` (« `_drop_module_routes` mutait une liste qu'il remplaçait »)
corrige un vrai défaut mais **n'était pas** la cause de cet échec : le code
mute bien en place et échoue quand même en ≥ 0.137.

Ce qui tient la frontière, faute de correctif :

- **`backend/test_versions_epinglees.py`** échoue si `fastapi.__version__` sort
  de `0.136.x`, avec un message qui dit quoi faire et renvoie vers la doc.
  Vérifié rouge en 0.137.0 et 0.141.1, vert en 0.136.1 et 0.136.3. Il vérifie
  aussi que `ci.yml` et `requirements.txt` n'épinglent pas des versions
  différentes.
- **Les deux tests de route fantôme** restent en place et passent sur la version
  épinglée : ils définissent le comportement voulu, le test de version garde la
  frontière.
- **`docs/limite-demontage.md`** — le symptôme mesuré, l'inspection verbatim des
  internes en 0.141, les tentatives et ce qu'elles ont appris, et les cinq
  options restantes avec leur coût. À lire avant tout bump. Y figure aussi ce
  qui n'a *pas* été mesuré.

Sur les tentatives, un point à ne pas résumer de travers : deux d'entre elles
**fonctionnent** (retirer l'entrée `_IncludedRouter`, ou filtrer récursivement
en invalidant le compteur au bon endroit) et font passer les tests en 0.141.
Elles n'ont pas été adoptées parce qu'elles écrivent du code contre des noms
privés absents de la version épinglée — donc à double chemin sur deux
dispositions internes à la fois — et parce que leur décision d'appartenance a
des trous mesurés. Le choix reste ouvert, options en §7 de la doc.

## 2026-08-08 — eslint bloquant en CI (cliquet à 63 avertissements)

`continue-on-error: true` neutralisait l'étape eslint : elle s'affichait verte
quoi qu'elle trouve. Un lint qu'on ne peut pas lire comme un verdict ne sert à
rien. Il est maintenant bloquant, avec `--max-warnings 63` — un cliquet : le
plafond ne peut que descendre, et quiconque ajoute un avertissement casse la CI.

Les 33 erreurs qui empêchaient de le rendre bloquant se répartissaient en deux
familles très différentes, que l'annotation GitHub ne distinguait pas :

- **11 étaient mécaniques** et sont corrigées. Six `no-explicit-any` dans
  `rangement` : quatre reçoivent un type honnête (l'API File System Access
  absente de la lib DOM, décrite par ce qu'on en consomme), trois annotations
  `catch (err: any)` redondantes sont retirées — `strict` étant absent de
  `tsconfig.app.json`, `catch (err)` donne déjà `any`, et le jour où `strict`
  passera à `true` ces lignes échoueront bruyamment au lieu de compiler en
  silence. La sixième, la charge utile SSE, garde un `any` **assumé** avec sa
  justification : sa forme dépend de l'événement et n'est définie que côté
  backend, sans schéma partagé — une union devinée aurait été un type faux,
  pire qu'un `any` explicite. Les quatre `no-unused-vars` (`_props`, `_t`,
  `_p`) sont réglées en alignant eslint sur le compilateur : `tsconfig.app.json`
  exempte déjà les identifiants préfixés `_`, eslint fait désormais pareil.
  Le `react-refresh/only-export-components` du chat est corrigé comme la règle
  le demande — les constantes partent dans `src/modules/chat/commands.ts`, ce
  qui rend le Fast Refresh au module de chat (jusqu'ici, éditer le composant
  rechargeait la conversation en cours au lieu d'en préserver l'état).

- **22 ne l'étaient pas** et sont passées en `warn` — voir ci-dessous.

### Dette assumée, non traitée par ce lot

- **22 vraies violations des Rules of React**, signalées par
  `react-hooks/set-state-in-effect` (10), `react-hooks/refs` (9) et
  `react-hooks/immutability` (3), apparues avec le preset
  `eslint-plugin-react-hooks@7` (règles React Compiler). **Ce ne sont pas des
  faux positifs** — contrairement aux 41 `exhaustive-deps` du même décompte, qui
  viennent de ce qu'eslint ne peut pas voir à travers `usePersistentState` que
  le setter retourné est celui de `useState`, donc stable. Elles sont réelles :
  setState synchrone dans un effet, lecture/écriture de ref pendant le rendu,
  accès à une variable avant sa déclaration.

  Elles ne sont pas corrigées parce que les corriger **change le comportement**,
  et qu'il n'existe **aucun test frontend** pour rattraper une régression
  (`package.json` n'a que `dev`/`build`/`lint`/`preview`, zéro `*.test.*`).

  **Prérequis avant d'y toucher : mettre en place `vitest` +
  `@testing-library/react`.** Puis commencer par
  **`src/usePersistentState.ts`** — il écrit `latest.current` et `keyRef.current`
  pendant le rendu, et ce hook porte l'état persisté de toute l'application :
  c'est à la fois la violation la plus structurante et celle dont la régression
  serait la plus diffuse. Le reste (chat, Atelier, code, settings) vient après.

## 2026-08-08 — Isolation : worker et tests committés, dépendances CI complétées

`backend/core/module_worker.py` et `backend/test_module_isolation.py` existaient
sur le disque sans être suivis par git. La découverte automatique des tests ne
voit que ce qui est commité : le test d'isolation ne tournait donc **jamais** en
CI, alors qu'il passait en local — la dette notée le 2026-08-07 est levée.

En les committant, un second défaut est apparu : `module_worker.py:290` fait
`import uvicorn` dans le sous-processus worker, et le job `backend` de
`ci.yml` n'installait pas `uvicorn`. Le symptôme aurait été trompeur — pas un
`ModuleNotFoundError` lisible, mais trois échecs
`le worker hello n'a pas démarré` : le sous-processus meurt à l'import, la sonde
de santé expire, et l'erreur remonte en timeout. Vérifié en rejouant
l'environnement exact du runner dans un venv isolé (144 tests OK avec `uvicorn`,
3 échecs sans). `uvicorn` est ajouté à la liste d'installation du job.

### Ce que ce lot ne fait PAS

L'isolation **n'est pas en vigueur**. Ce lot ne fait que verser le worker et son
test au dépôt. `core/module_registry.py:95` importe toujours tous les routers
dans le process principal, `spawn_worker` n'est appelé que par les tests, et
aucune route `/capabilities/*` n'existe. Les conditions de CLAUDE.md §7 pour
déclarer l'isolation faite restent non remplies : un module généré tourne
toujours avec `os.environ` (clés API) et l'accès au token d'instance.

## 2026-08-07 — CI : découverte automatique des tests, build frontend bloquant

Le job `backend` lançait les tests **un par un, nommément**. Quatre fichiers sur
six ne tournaient donc jamais, sans que rien ne le signale : il suffisait
d'oublier une ligne. Il tourne maintenant en
`python -m unittest discover -s . -p 'test_*.py' -v` — un nouveau
`backend/test_*.py` est pris en compte sans toucher au workflow.

`test_modules_mount.py` est renommé `integration_modules_mount.py` : il charge
`core.runtime` (torch, chromadb, sentence-transformers) et n'a rien à faire dans
le job léger. Le préfixe suffit à l'exclure du motif de découverte — pas de
`skipUnless` ni de variable d'environnement à se rappeler. Il reste lancé par le
job `integration` (manuel).

Le job `frontend` fait désormais `npm run build` de façon **bloquante** :
`tsc --noEmit` ne couvre pas les erreurs de bundling, et le TSX généré par
l'Atelier atterrit dans `frontend/src/modules/generated/` (inclus dans
`tsconfig.app.json`). Un module généré qui ne bundle pas cassait l'image
frontend sans que la CI le voie.

### Dette assumée, non traitée par ce lot

- **`eslint` reste en `continue-on-error`** et **`"strict": true` reste absent
  de `tsconfig.app.json`**. Les deux sont de vraies dettes, mais les résorber
  implique de toucher ~94 erreurs (règles `react-hooks`) sur tout le codebase :
  c'est un chantier séparé, pas du durcissement. Tant que ce point n'est pas
  traité, un warning eslint ne fait pas échouer la CI — ne pas s'y fier.
- **`backend/test_module_isolation.py` n'est pas suivi par git**, donc la
  découverte automatique ne le voit pas en CI (elle ne voit que ce qui est
  commité). C'est le chantier d'isolation des modules générés, cf. CLAUDE.md §7.

## 2026-07-02 — Purge des données personnelles de l'historique git

L'intégralité de l'historique (toutes branches) a été réécrite avec
`git filter-repo --invert-paths` pour supprimer définitivement les données
personnelles de runtime, avant partage du dépôt :

- `backend/history/` (conversations sauvegardées)
- `backend/memory/` (profil élève, sessions, flashcards, quotas…)
- `backend/doc_uploads/` (documents uploadés)
- `backend/chroma_db/` (index vectoriel des fiches)
- `workspace/` (sorties du code-agent — oublié par la première passe,
  purgé par une seconde réécriture le même jour)

Ces chemins n'étaient plus suivis depuis `b8575c1` (ils sont recréés
automatiquement au démarrage — un clone frais fonctionne sans eux) ; cette
réécriture retire aussi les versions historiques. **Tous les hashes de commit
ont changé** : les clones antérieurs au 2026-07-02 doivent être re-clonés
(pas de pull — l'historique a divergé volontairement). Une sauvegarde miroir
complète de l'état antérieur est conservée localement, hors dépôt.
