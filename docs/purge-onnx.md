# Purge du modèle Piper de l'historique git

**Objectif.** `.git` pèse 71 Mo à cause de
`backend/piper_models/fr_FR-upmc-medium.onnx` (76 Mo). Chaque personne qui clone
les paie, pour un fichier qui n'est pas du code et qui se retélécharge.

**Statut.** Dernière opération irréversible avant partage. À faire **avant** que
quiconque clone : une réécriture d'historique après coup obligerait tout le
monde à re-cloner.

**Deux commits, dans cet ordre strict.** Le téléchargement doit fonctionner et
être vérifié **avant** la purge — sinon un clone frais se retrouve sans voix et
sans moyen de l'obtenir.

---

## Commit A — récupérer le modèle à l'exécution

### A.1 Résolution du chemin

`backend/core/voice.py:41` :

```python
def __init__(self, voice: str = "fr_FR-upmc-medium", models_dir: str = "piper_models"):
```

Chemin relatif, résolu contre le cwd. Ça ne fonctionne que parce que
`epure_tray.py` lance uvicorn depuis `backend/`. Ajoute `resolve_models_dir()`
dans `core/paths.py` — `$EPURE_MODELS_DIR`, sinon `<backend>/piper_models`,
résolu **au point d'appel**. Même vigilance que pour `resolve_data_dir()` : pas
de valeur figée dans un défaut d'argument.

Ce n'est pas un dossier de données utilisateur mais un cache de modèles : il ne
doit **pas** être empreinté par `test_zz_donnees_reelles` (un téléchargement
légitime ferait tomber le garde-fou). Dis-le dans la docstring.

### A.2 Téléchargement à la demande

Le modèle est téléchargé **au premier usage de la synthèse vocale**, pas au
démarrage. `PiperEngine` est déjà derrière un `_LazyEngine` : la contrainte est
donc déjà en place, ne la casse pas.

Deux fichiers à récupérer ensemble, `fr_FR-upmc-medium.onnx` et
`.onnx.json` — ce sont une paire, un décalage de version entre les deux serait
silencieux.

**Vérifie l'URL par une requête réelle avant de l'écrire en dur.** Les voix
Piper sont publiées sur le dépôt HuggingFace `rhasspy/piper-voices` ; je ne te
donne pas le chemin exact de mémoire, parce que je me suis trompé quatre fois
aujourd'hui en énonçant des valeurs plausibles sans les mesurer. Fais un `HEAD`,
constate le code de retour et la taille, et note l'URL retenue dans un
commentaire avec la date de vérification.

### A.3 Intégrité

Ne dépends pas d'un hash publié en amont. Calcule le **sha256 du fichier actuel
sur mon disque** — celui qui fonctionne — et écris-le en constante. Tout
téléchargement est vérifié contre lui ; en cas d'écart, le fichier est supprimé
et une erreur claire est levée. C'est auto-suffisant et ça détecte aussi bien
une corruption réseau qu'un changement de contenu en amont.

### A.4 Dégradation

Sans réseau, ou si le téléchargement échoue : la synthèse vocale est
indisponible avec un message explicite, **et rien d'autre ne casse**. Épure est
local-first ; la voix est optionnelle et doit le rester. Vérifie-le par un test
qui simule l'échec réseau et constate que l'app répond normalement sur tout le
reste.

Prévois aussi le retour utilisateur : 76 Mo téléchargés sans prévenir est
désagréable. Un message avant, ou une progression, à toi de voir — dis-moi ce
que tu proposes.

### A.5 Reste à faire

- `backend/piper_models/` ajouté au `.gitignore`.
- README : la voix télécharge son modèle au premier usage, taille, et le
  comportement hors ligne.
- Un test qui échoue si `resolve_models_dir()` est figé (même forme que
  `test_data_dir.py`).

**Vérification obligatoire avant de passer au commit B** : renomme
temporairement `backend/piper_models/` et lance une synthèse vocale. Le modèle
doit se télécharger et la voix fonctionner. Sans cette preuve, la purge est
prématurée.

Commit : `feat(voice): telecharge le modele piper a la demande`

---

## Commit B — la purge

**Irréversible. Ne commence pas sans avoir fait la sauvegarde.**

### B.1 Sauvegarde

```powershell
cd C:\Users\Ilyan
git clone --mirror epure epure-backup-avant-purge-onnx.git
```

Hors du dépôt, conservé jusqu'à ce que tout soit vérifié. Le CHANGELOG du
2026-07-02 mentionne qu'une sauvegarde miroir avait déjà été faite pour la purge
précédente — même geste.

### B.2 La réécriture

`git filter-repo` refuse par sécurité de travailler sur un dépôt non fraîchement
cloné. Travaille sur un clone dédié plutôt que de forcer :

```powershell
cd C:\Users\Ilyan
git clone epure epure-purge
cd epure-purge
git filter-repo --path backend/piper_models/fr_FR-upmc-medium.onnx --path backend/piper_models/fr_FR-upmc-medium.onnx.json --invert-paths
```

`filter-repo` retire le remote `origin` volontairement, pour éviter un push
accidentel. Il faut le remettre à la main.

### B.3 Vérifications AVANT de pousser

Dans `epure-purge`, toutes doivent passer :

- `git rev-list --objects --all | Select-String onnx` → **aucune sortie**
- taille de `.git` → doit être tombée sous 5 Mo
- `git log --oneline | Measure-Object -Line` → même nombre de commits qu'avant
- la suite de tests passe (avec le modèle téléchargé par le commit A)
- `npm ci && npm run build` passe

Si l'une échoue, jette `epure-purge` et reviens me voir. Ne pousse rien.

### B.4 Publication

```powershell
git remote add origin https://github.com/ilyanndx-lab/epure.git
git push --force --all
git push --force --tags
```

Puis remplace ton dépôt de travail par le dépôt purgé — ne tente pas de
réconcilier l'ancien par un `pull`, les historiques ont divergé volontairement.

### B.5 Conséquence à assumer et à écrire

**Tous les hashes de commit changent.** Or `CHANGELOG.md`,
`docs/limite-demontage.md`, `docs/rapport-verif.md` et plusieurs messages de
commit citent des hashes (`64a5636`, `8fdc31c`, `e498e65`…). Ils deviendront
pendants.

Ne les réécris pas — ce serait interminable et faux au commit suivant. Ajoute
une entrée CHANGELOG qui dit que les hashes antérieurs à la purge ne résolvent
plus, et que les commits restent trouvables par recherche textuelle du sujet.
C'est la même note que celle du 2026-07-02, qui a déjà servi une fois.

Note aussi que GitHub ne libère pas immédiatement l'espace côté serveur : les
anciens objets subsistent jusqu'à un ramasse-miettes que tu ne contrôles pas.
Un clone frais, lui, est immédiatement léger — c'est ce qui compte.

Commit (dans le dépôt purgé, avant le push) :
`docs: purge du modele piper de l'historique (hashes reecrits)`

---

## Après

Vérification finale, depuis un dossier temporaire :

```powershell
git clone https://github.com/ilyanndx-lab/epure.git epure-verif-purge
```

Mesure `.git`, lance la suite, lance le build. C'est ce que recevront tes
proches. Supprime ensuite `epure-purge`, `epure-verif-purge`, et la sauvegarde
miroir seulement une fois que tu es sûr.
