# Épure — Détails de l'atelier de dev (venv, verif-ci.ps1, suite de tests)

> Extrait de `CLAUDE.md` (§2 Lancer et tester), déplacé ici le 2026-09-20 pour alléger
> le noyau chargé à chaque session. Contenu inchangé, seul l'emplacement a
> changé — voir `CLAUDE.md` pour le renvoi et la règle résumée.

---

### Interpréteur Python — venv dédié, géré par `dev-epure.ps1`

Le backend tourne dans un **venv privé à Épure**, `.venv/` à la racine du dépôt
(déjà dans `.gitignore`, et hors de portée de `tools/faire_paquet.py` — ce
script ne copie que `backend/` et `frontend/dist`, jamais la racine du dépôt).
Avant, `backend/` tournait sur le Python **partagé** de la machine, celui
qu'utilisent aussi les autres projets d'Ilyann : chaque nouveau lot de
dépendances ML d'Épure (`torch`/`transformers`/`optimum-onnx` pour
`core/hmer.py` notamment) forçait une enquête de dépendances inverses sur les
paquets **des autres projets** avant de savoir si une rétrogradation les
casserait. Un venv dédié supprime ce risque structurellement.

`tools\dev-epure.ps1` **crée et synchronise ce venv tout seul** : absent, il le
crée (n'importe quel Python du poste sait faire `-m venv`, il n'est plus
nécessaire ensuite) ; présent, il relit `requirements.txt` à chaque lancement
(`pip install -r`, silencieux quand tout est déjà à jour) puis vérifie
réellement que `fastapi`/`uvicorn` s'importent avant de continuer. Objectif :
ne plus jamais se demander quel Python est actif ici.

`$env:EPURE_PYTHON` reste l'échappatoire pour pointer sur un **autre**
interpréteur (débogage, comparaison de versions) : quand elle est posée, la
gestion du venv est court-circuitée et l'interpréteur nommé est utilisé tel
quel — mais toujours vérifié, un mauvais chemin doit échouer nommé.

**`epure_tray.py` (l'usage normal, lancé par le raccourci de bureau) lance
désormais aussi le backend dans ce venv dédié.** Ce n'était pas le cas avant le
2026-09-08 : le tray passait `sys.executable` — l'interpréteur qui le lance
lui-même, le Python **partagé**, sauf raccourci repointé — tel quel à uvicorn,
laissant le risque de rétrogradation de dépendances ouvert sur le seul chemin
d'usage réel (`dev-epure.ps1` sert au développement, pas à l'usage quotidien).
`lanceur.py` (le module partagé entre le tray et sa logique testable, cf.
« Quatre lanceurs, quatre publics » plus bas) porte `assurer_venv_backend()` :
au premier lancement sur
un poste neuf, il **crée** `.venv/` et y installe `requirements.txt`, comme
`dev-epure.ps1` ; ensuite il se contente de le **réutiliser**, sans
resynchroniser à chaque démarrage — volontairement asymétrique avec
`dev-epure.ps1`, qui est le workflow « après un `git pull` », pas le tray.
`$env:EPURE_PYTHON` reste la même échappatoire des deux côtés, rendue telle
quelle sans création ni vérification côté résolution — seulement vérifiée après
coup (`fastapi`/`uvicorn` importables), comme dans le script PowerShell.
**Aucun repli sur `sys.executable` si la résolution échoue** : ce serait
réintroduire, en silence, le risque que ce venv existe pour supprimer — le
backend ne démarre pas et l'incident est journalisé (`epure_tray.log`,
infobulle de l'icône).

Ce que ce changement ne couvre PAS : le **process du tray lui-même**
(`epure_tray.py`, celui qui importe `pystray`/`PIL`) reste sur l'interpréteur
qui le lance — le Python partagé, sauf si le raccourci de bureau est repointé
sur `.venv\Scripts\pythonw.exe`, ce qui n'a pas été fait. `pystray` et `Pillow`
doivent donc rester installés sur le Python partagé, sans quoi le tray lui-même
ne démarre plus — seul son enfant uvicorn a changé d'interpréteur.

De même, `core/codeagent.py` (module Code) exécute les scripts de l'utilisateur
et installe leurs paquets opt-in (`POST /code/install`) sur `sys.executable` du
process qui tourne — donc sur le venv dédié quand le backend y tourne, que ce
soit via `dev-epure.ps1` ou désormais via `epure_tray.py`, et sur le Python
partagé seulement si `$env:EPURE_PYTHON`/`EPURE_PYTHON` pointe ailleurs. Un
paquet installé par ce biais pour un script (`matplotlib`, par exemple —
support dédié dans `core/_plot_support/`, dépendance **volontairement** absente
de `requirements.txt`, cf. `test_codeagent_plots.py`) ne traverse pas d'un
interpréteur à l'autre : il se réinstalle depuis le panneau du module Code si
besoin.

### Avant de pousser — `tools\verif-ci.ps1`

```powershell
.\tools\verif-ci.ps1              # frontend + backend, périmètre de la CI
.\tools\verif-ci.ps1 -Frontend    # ou -Backend
```

**`verif-ci.ps1` n'est PAS conscient du venv** : sa commande de tests backend
vient de `ci.yml` (`python -m unittest discover …`), donc `python` y est résolu
sur le PATH de la session PowerShell **courante**, pas via `$env:EPURE_PYTHON`
ni via `.venv/`. L'activer d'abord (`.\.venv\Scripts\Activate.ps1`) avant de
lancer `-Backend` — sinon la mesure porte sur le Python partagé, un « vert »
qui ne dit rien du venv réellement utilisé par le backend (cf. l'avertissement
juste en dessous sur ce que ce script mesure et ne mesure pas).

**Version de Node — `frontend/.nvmrc` est la seule source de vérité.** La CI
la lit (`node-version-file` dans `ci.yml`) et `verif-ci.ps1 -Frontend` échoue
si le Node du poste n'a pas la même version majeure. Incident à l'origine
(2026-09-22) : CI figée sur 22 depuis sa création, poste et `install.ps1` en 24 ;
9 tests du module Image verts ici, rouges là-bas (l'undici de Node 22 appelle
`Blob.stream()`, absent du `Blob` de jsdom). Changer de version = changer ce
fichier, jamais `ci.yml`. `engines.node` de `frontend/package.json` (`24.x`,
2026-09-23) en est le MIROIR, pour qu'npm avertisse (`EBADENGINE`) — le changer
en même temps : rien ne vérifie encore que les deux restent alignés.

**IMPÉRATIF — `npm run lint` et la suite backend lancés à la main ne mesurent
PAS ce que mesure la CI.** Deux fois le 2026-09-05, un « vert en local » est
parti rouge en CI, sur deux axes indépendants : la CI installe **tout le
catalogue** dans `frontend/src/modules/generated/` avant de linter (51
avertissements ici, 62 là-bas, pour un cliquet à 61), et son clone n'a que les
modules **versionnés** dans `backend/modules/` (un module de catalogue installé
sur le poste faisait échouer `test_catalogue` ici et passer là-bas). Le seul
contrôle qui vaut avant de pousser est ce script : il **lit `ci.yml`** au lieu de
le paraphraser — cliquet eslint et commande de tests en sont extraits, et une
lecture qui échoue **arrête** au lieu de retomber sur un défaut — et il travaille
dans un arbre **temporaire**, jamais dans `generated/` (la CI y fait un `rm -rf`
qui, ici, emporterait les modules réellement installés).

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
python test_arbre_modules_deterministe.py  # l'arbre de _test_env ne dépend pas du poste (§3.5)
python test_web_search.py         # recherche web, HTTP mocké
python test_web_statique.py       # interface servie par FastAPI + EPURE_ATELIER=0
python test_chat_conversation_id_trames.py  # chaque trame /ws/chat porte conversation_id (§3.6)
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
python test_chat_vision_ciblee.py # analyse vision CIBLÉE dans le chat, cache par fichier, @image (§3.3 ter)
python test_hmer.py               # transcription manuscrite : rendu, recadrage, poids (§3.8)
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
`epure_tray.py` (usage normal : icône, Ollama, Vite, console masquée — le
backend qu'il lance tourne dans le même venv dédié que `dev-epure.ps1`,
cf. section 2, créé tout seul au premier lancement),
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

Le venv dédié (section 2) tourne en **Python 3.14** — la même version que
portait déjà le Python partagé avant lui, choisie pour ne pas ajouter une
variable à un écart qui existe déjà : la CI tourne en **3.12** (`ci.yml`). Du
code qui marche en local peut casser en CI. Si tu utilises une syntaxe
récente, vérifie sa disponibilité en 3.12. Le venv dédié isole Épure des
*autres projets* du poste ; il ne rapproche pas la version locale de celle de
la CI, et ce n'est pas son rôle.

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

