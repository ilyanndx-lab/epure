# Épure — Résolution des chemins (core/paths.py), détail

> Extrait de `CLAUDE.md` (§3.5), déplacé ici le 2026-09-20 pour alléger
> le noyau chargé à chaque session. Contenu inchangé, seul l'emplacement a
> changé — voir `CLAUDE.md` pour le renvoi et la règle résumée.

---

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
- `resolve_hmer_dir()` — `$EPURE_HMER_DIR`, sinon `<backend>/hmer_model`. Troisième
  jumeau des deux précédents : cache de 117,7 Mo (`pix2text-mfr`, deux `.onnx` et
  six fichiers de configuration), téléchargé au premier usage sur une révision
  HuggingFace **épinglée** et vérifié par sha256. Détourné par `_test_env`,
  **absent** de `REAL_DIRS`. Ne pas le ranger avec `resolve_encre_dir()` sous
  prétexte qu'ils appartiennent au même module : l'encre est irremplaçable, les
  poids se retéléchargent à l'octet (§3.8).
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
testable : son `rmtree` frappe la copie.

**IMPÉRATIF — la copie écarte les modules installés sur CE poste**, c'est-à-dire
tout manifeste d'`origin` `catalogue` ou `workshop` (`_test_env.MODULES_DU_POSTE`,
appliqué aux DEUX moitiés : `backend/modules/` et `frontend/src/modules/generated/`,
que `catalogue.install()`/`uninstall()` écrivent et retirent ensemble). Sans ça
l'arbre de test dépend de ce que l'utilisateur a installé, et la suite ne mesure
pas la même chose ici et en CI — payé par
`test_catalogue.test_catalogue_liste_les_six_avec_installe`, rouge en permanence
sur le poste de dev et vert en CI parce que le module `code` y était réellement
installé : `installé: True` était la bonne réponse à une question posée au
mauvais arbre. **Ce n'était pas un problème d'ordre d'exécution** — il échouait
seul, dans les deux sens de n'importe quelle paire — et c'est la fausse piste
qui l'a fait survivre. Le défaut est de COPIER : un dossier sans manifeste
(`_atelier`, un reliquat de désinstallation) ou un manifeste sans `origin` reste
copié, mieux vaut un arbre trop riche qu'un module versionné disparu en silence.
Verrouillé par `test_arbre_modules_deterministe.py`, qui tient aussi la liaison
tardive de `resolve_modules_dir()`. `EPURE_MODELS_DIR`, `EPURE_EMBEDDING_DIR`,
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

