# Vérification du durcissement — script d'exécution et de contrôle

Document exécutable par Claude Code. Objectif : finir les commits en attente,
puis **vérifier par l'exécution** que la branche `hardening/v1` est dans l'état
annoncé — et produire un rapport que je puisse relire.

## Règles de conduite

- **Jamais `git add -A`, jamais `git add .`, jamais `git checkout -- .`.** L'arbre
  de travail de ce dépôt a déjà été un piège ; on ne stage que des chemins
  explicites.
- **Ne rien « réparer » au-delà du périmètre décrit.** Si une vérification
  échoue en dehors de ce que la §2 autorise à corriger, **arrête-toi et
  reporte** — l'échec est une information, pas un obstacle.
- Tout ce qui est écrit dans un bloc ```` ```powershell ```` est une commande.
  Tout ce qui est écrit dans un bloc ```` ```yaml ```` ou ```` ```gitattributes ```` est du **contenu de
  fichier**, à écrire avec l'éditeur — jamais à coller dans un terminal.
- Les messages de commit sont **sans accents** : ce poste est sous PowerShell 5.1
  et le dépôt a déjà été mordu deux fois par des problèmes d'encodage.

---

## §1 — État attendu au démarrage

Avant de commencer, vérifier :

```powershell
cd C:\Users\Ilyan\epure
git branch --show-current
git log --oneline -8
git status --porcelain
```

Attendu : branche `hardening/v1`, et un `git status` contenant exactement
`?? backend/core/module_worker.py`, `?? backend/test_module_isolation.py`,
`?? CLAUDE.md`, `?? docs/`, plus éventuellement `?? docs/verif-hardening.md`
(ce fichier). **Si autre chose apparaît, arrête et reporte.**

---

## §2 — Corrections à appliquer

### 2.1 — `uvicorn` manquant dans les dépendances CI

`backend/core/module_worker.py:290` fait `import uvicorn` dans le sous-processus
worker. Le job `backend` de `.github/workflows/ci.yml` ne l'installe pas :
`test_module_isolation.py` passerait en local et échouerait en CI.

Éditer `.github/workflows/ci.yml`, job `backend`, étape d'installation, pour que
la liste devienne :

```yaml
        run: >
          pip install fastapi pydantic pyyaml python-dotenv
          httpx python-multipart chromadb ollama pypdf watchdog uvicorn
```

### 2.2 — Autres dépendances manquantes révélées par la §3.2

La §3.2 rejoue l'environnement de la CI dans un venv isolé. Si elle révèle
d'autres imports manquants, **les ajouter à la même ligne** de `ci.yml` et le
noter dans le rapport. C'est la seule correction autorisée hors de la 2.1.

---

## §3 — Vérifications

Chaque vérification produit un verdict OK / ÉCHEC à reporter, avec la sortie
réelle des commandes (pas un résumé).

### 3.1 — Suite de tests locale, commande exacte de la CI

```powershell
cd C:\Users\Ilyan\epure\backend
python -m unittest discover -s . -p "test_*.py" -v
cd ..
```

Attendu : tous les tests passent. Reporter le nombre total de tests et la durée.
`integration_modules_mount.py` ne doit **pas** être ramassé par la découverte
(son préfixe l'exclut) — le confirmer.

### 3.2 — Rejouer l'environnement de la CI (le contrôle qui compte)

En local, `requirements.txt` est installé en entier, donc un import manquant
dans `ci.yml` ne se voit pas. On recrée l'environnement exact du runner :

```powershell
cd C:\Users\Ilyan\epure
python -m venv $env:TEMP\epure-ci-venv
& "$env:TEMP\epure-ci-venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$env:TEMP\epure-ci-venv\Scripts\python.exe" -m pip install --quiet fastapi pydantic pyyaml python-dotenv httpx python-multipart chromadb ollama pypdf watchdog uvicorn
cd backend
& "$env:TEMP\epure-ci-venv\Scripts\python.exe" -m unittest discover -s . -p "test_*.py" -v
cd ..
```

Si un `ModuleNotFoundError` apparaît, c'est exactement le bug que la CI aurait
révélé : ajouter le paquet manquant à `ci.yml` **et** à la commande ci-dessus,
relancer, et le signaler dans le rapport.

Nettoyage :

```powershell
Remove-Item -Recurse -Force $env:TEMP\epure-ci-venv
```

### 3.3 — Build frontend

```powershell
cd C:\Users\Ilyan\epure\frontend
npm run build
cd ..
```

Attendu : succès. Reporter la durée et la taille du bundle. Si des warnings
apparaissent, les reporter sans les corriger.

---

## §4 — Commits

Uniquement si §3.1, §3.2 et §3.3 passent. Sinon, s'arrêter et reporter.

```powershell
cd C:\Users\Ilyan\epure
git add .github/workflows/ci.yml backend/core/module_worker.py backend/test_module_isolation.py
git commit -m "feat(isolation): worker isole + tests, deps CI completees"
git add CLAUDE.md docs/
git commit -m "docs: CLAUDE.md + plans de durcissement, catalogue et verification"
git status --porcelain
```

Attendu : `git status --porcelain` **vide**. S'il ne l'est pas, reporter son
contenu sans rien ajouter.

Mettre aussi à jour `CHANGELOG.md` avec une entrée datée pour ce lot (isolation
committée, deps CI complétées), dans le style des entrées existantes : dire le
symptôme, pas seulement la cause. Commit séparé :
`docs: entree changelog isolation + deps CI`.

---

## §5 — Contrôle du clone à froid

C'est ce que recevront les personnes à qui le dépôt sera partagé. Le vérifier
sur un clone réel, pas sur l'arbre de travail.

```powershell
cd C:\Users\Ilyan\epure
Remove-Item -Recurse -Force $env:TEMP\epure-clone -ErrorAction SilentlyContinue
git clone --branch hardening/v1 --no-local . $env:TEMP\epure-clone
cd $env:TEMP\epure-clone
git log --oneline -5
Get-ChildItem backend\modules -Name
Get-ChildItem frontend\src\modules\generated -Name
git status --porcelain
"{0:N1} Mo" -f ((Get-ChildItem -Recurse -Force .git | Measure-Object Length -Sum).Sum / 1MB)
```

À vérifier et reporter :

1. `backend/modules` contient exactement : `_atelier, admin, chat, code, docs,
   flashcards, hello, history, kholle, rangement, reviseur, settings`.
   **Aucun** des neuf modules de test (astral, clicker, dinosaure, emojis,
   minecraft, minuteur, pong, snake, vroom).
2. `frontend/src/modules/generated` contient exactement `hello` et `rangement`.
3. `git status --porcelain` dans le clone est **vide** — c'est le contrôle du
   `.gitattributes` : si des fichiers apparaissent modifiés juste après un
   clone, la normalisation des fins de ligne n'est pas correcte.
4. Aucune donnée personnelle. Le confirmer explicitement :

```powershell
cd $env:TEMP\epure-clone
git log --all --oneline -- backend/history backend/memory backend/doc_uploads backend/chroma_db workspace
git rev-list --objects --all | Select-String -Pattern "TIPE|profile\.json|conversations\.json|instance_config|\.env$"
```

Attendu : **aucune sortie** pour les deux commandes.

5. Le build passe depuis le clone :

```powershell
cd $env:TEMP\epure-clone\frontend
npm ci
npm run build
```

6. Taille de `.git` (l'ONNX de 76 Mo est connu et non traité — noter la valeur).

Nettoyage :

```powershell
cd C:\Users\Ilyan\epure
Remove-Item -Recurse -Force $env:TEMP\epure-clone
```

---

## §6 — Rapport à produire

Écrire le rapport dans `docs/rapport-verif.md` **et** l'afficher en fin de
session pour qu'il puisse être copié. Format exact, une ligne par contrôle,
verdict `OK` / `ÉCHEC` / `NON FAIT` suivi du fait constaté — jamais d'appréciation
sans chiffre :

```
# Rapport de vérification — hardening/v1
Date :
Dernier commit :

## Corrections appliquées
- uvicorn ajouté à ci.yml :
- autres deps ajoutées (§2.2) :

## Vérifications
- §3.1 tests locaux (discover)      : OK/ÉCHEC — N tests, Xs
- §3.2 environnement CI rejoué      : OK/ÉCHEC — N tests, deps manquantes trouvées :
- §3.3 npm run build (arbre local)  : OK/ÉCHEC — Xs
- §4   git status vide après commits: OK/ÉCHEC —
- §5.1 modules du clone conformes   : OK/ÉCHEC — liste réelle :
- §5.2 generated = hello, rangement : OK/ÉCHEC — liste réelle :
- §5.3 git status vide dans le clone: OK/ÉCHEC —
- §5.4 aucune donnée personnelle    : OK/ÉCHEC —
- §5.5 npm ci + build depuis le clone: OK/ÉCHEC — Xs
- §5.6 taille de .git               : ... Mo

## Anomalies rencontrées
(tout écart, même corrigé ; sinon « aucune »)

## Ce que je n'ai pas pu vérifier
(la CI n'a pas tourné : gh n'est pas installé et la branche n'est pas poussée —
le préciser si c'est toujours le cas)

## Sorties brutes
(coller la sortie de unittest discover §3.2, de npm run build §5.5,
et du git status du clone §5.3)
```

Ne pas embellir. Un `ÉCHEC` documenté vaut mieux qu'un `OK` obtenu en changeant
la vérification.
