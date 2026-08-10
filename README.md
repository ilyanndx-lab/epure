# Épure

Assistant d'étude et de travail local-first : chat multi-modèles (Ollama local +
fournisseurs cloud optionnels), analyse documentaire (RAG sur vos PDF),
historique consultable et synthèse/transcription vocale.

Le cœur ne présume rien de votre domaine. Ce qui spécialise une instance, ce
sont ses **modules**, installables à la demande depuis le catalogue : flashcards,
kholles, agent de code, tri de documents, révision espacée — et ceux que vous
ferez générer par l'**Atelier**.

- **Backend** : FastAPI (`backend/`)
- **Frontend** : React + Vite + TypeScript (`frontend/`)
- **LLM local** : [Ollama](https://ollama.com) (cloud optionnel : Gemini, Groq, Cerebras, Mistral, NVIDIA)

> 📖 **Vous voulez juste vous servir d'Épure ?** Le [guide
> d'utilisation](docs/guide.md) s'adresse à vous : installation en un fichier,
> premiers pas, modules, dépannage — sans terminal. Ce README-ci s'adresse à qui
> installe à la main, développe ou déploie.

---

## Installation en un fichier (Windows)

### 1. Git, puis un terminal neuf

`install.ps1` vit **dans** le dépôt : il faut donc l'avoir cloné pour pouvoir le
lancer, et aucun script ne peut automatiser une étape qu'il faudrait déjà avoir
franchie pour s'exécuter.

```powershell
winget install Git.Git
```

**Fermez ensuite le terminal et rouvrez-en un autre** : le `PATH` n'est rafraîchi
qu'au nouveau shell, et `git` reste introuvable dans celui qui vient de
l'installer.

```powershell
git clone <URL du dépôt> epure
cd epure
```

> 🚫 **Ne prenez pas le bouton « Download ZIP » de GitHub.** Un ZIP n'a pas de
> dossier `.git`, donc pas de `git pull` : la section [Mettre à
> jour](#mettre-à-jour) ne s'applique plus du tout. Il faudrait re-télécharger
> l'archive entière à chaque version et la déballer par-dessus l'installation
> existante, au risque d'écraser vos données, vos documents et vos modules.
> Clonez.

### 2. L'installeur

Dans le dossier du projet, clic droit sur `install.ps1` → **Exécuter avec
PowerShell**. Le script installe ce qui manque (Python 3.12, Node.js LTS,
Ollama, le modèle de `backend/config.yaml`), pose les dépendances, crée
`backend/.env` s'il n'existe pas, et ajoute un raccourci **Épure** sur le
Bureau.

```powershell
.\install.ps1 -DryRun   # affiche chaque décision, n'installe rien
.\install.ps1           # installe
```

Il est **idempotent** : le relancer détecte ce qui est déjà présent, ne réécrit
aucun fichier existant, et sert donc aussi de mise à jour. Journal dans
`install.log`, code de sortie non nul dès qu'une étape échoue.

> ⚠️ Ce script n'a **jamais tourné sur une machine vierge** à ce jour. Sur un
> poste déjà équipé, chaque détection répond « déjà présent » et ne prouve rien
> des chemins d'installation. Lancez `-DryRun` d'abord.

**Clés API** : aucune n'est nécessaire — sans clé, Épure tourne sur Ollama local,
et c'est le cas nominal. Pour un fournisseur cloud, mettez **vos** clés dans le
`backend/.env` que l'installeur a copié depuis `.env.example` (cf. [Clés API
cloud](#clés-api-cloud-optionnelles)) ; ne réutilisez jamais le `.env` de
quelqu'un d'autre.

Les sections ci-dessous décrivent l'installation manuelle, qui reste le repli.

---

## Démarrage rapide (Docker)

Prérequis : Docker + Docker Compose v2.24+.

```bash
git clone <repo> epure && cd epure

# (optionnel) clés API cloud et options — sinon, modèles locaux uniquement
cp backend/.env.example backend/.env   # puis éditez si besoin

docker compose up --build
```

- Frontend : <http://localhost:5173>
- API : <http://localhost:8000> (docs interactives : <http://localhost:8000/docs>)

Au premier démarrage, le backend télécharge ses modèles ML (embeddings,
Whisper) ; comptez quelques minutes. Le healthcheck patiente jusqu'à
5 min avant de considérer le service en échec. Le modèle de **synthèse vocale**,
lui, n'arrive qu'au premier usage de la voix (cf. plus bas) — le démarrage n'en
dépend pas.

### Ollama avec Docker

Par défaut, le backend conteneurisé contacte l'Ollama **de votre machine hôte**
(`host.docker.internal:11434`). Installez Ollama localement et démarrez-le :

```bash
ollama serve
ollama pull qwen2.5:7b      # modèle par défaut (cf. backend/config.yaml)
```

Pour utiliser un Ollama **embarqué dans Compose** à la place :

```bash
OLLAMA_HOST=http://ollama:11434 docker compose --profile ollama up --build
# puis, une fois lancé :
docker compose exec ollama ollama pull qwen2.5:7b
```

> ⚠️ `OLLAMA_HOST` doit être une URL complète (`http://hôte:11434`).
> Ne le réglez jamais sur `0.0.0.0` : cela casse le client Python Ollama.

### Vos fiches PDF

Le dossier `./data/fiches` (monté en volume) est la racine des fiches. Déposez-y
vos PDF, organisés dans des sous-dossiers `Maths/`, `Physique-Chimie/`, `SI/`
(les dossiers surveillés sont configurés dans `backend/config.yaml`).

---

## Installation locale (développement)

Prérequis : Python 3.12+, Node.js 20+, et Ollama.

### 1. Ollama (modèles locaux)

```bash
# Installez Ollama depuis https://ollama.com puis :
ollama serve
ollama pull qwen2.5:7b
```

### 2. Backend (FastAPI)

```bash
cd backend
python -m venv .venv
# Windows : .venv\Scripts\activate   |   macOS/Linux : source .venv/bin/activate
pip install -r requirements.txt

# (optionnel) clés API et options
cp .env.example .env   # puis éditez

python -m uvicorn main:app --reload
```

L'API écoute sur <http://localhost:8000>.

### 3. Frontend (React + Vite)

```bash
cd frontend
npm install
npm run dev
```

L'interface est servie sur <http://localhost:5173> (CORS autorisé par défaut
pour cette origine).

### Lanceur Windows tout-en-un

```powershell
python epure_tray.py
```

Démarre Ollama, le backend et le frontend, ouvre le navigateur, et pose une
icône dans la zone de notification (Ouvrir / Redémarrer / Quitter). Les
dépendances du lanceur (`pystray`, `Pillow`) sont dans
`backend/requirements.txt`. Le journal part dans `epure_tray.log`.

Windows uniquement : le script utilise `subprocess.STARTUPINFO`, `taskkill` et
`netstat`. Sous Linux/macOS, lancez les trois services à la main (sections
ci-dessus).

> **Ollama doit être installé** avant le premier lancement. S'il est absent du
> PATH, le lanceur pose bien son icône mais s'arrête en silence sur
> `Popen(["ollama", "serve"])` : ni backend, ni frontend, ni navigateur, et
> `epure_tray.log` s'interrompt après `Lancement ollama serve` sans message
> d'erreur. Contrairement à `flm`, qui est optionnel et dont l'absence est
> attrapée.

Ce lanceur remplace `start.ps1`, retiré du dépôt : il n'était utilisable que sur
le poste de son auteur.

- **Chemins absolus en dur** (`cd C:\Users\Ilyan\epure\backend`) — faux chez
  tout le monde sauf lui.
- **Lancement inconditionnel de `flm`**, un runtime NPU propre à une machine
  précise, sans garde si le binaire est absent. `epure_tray.py` le traite comme
  ce qu'il est : optionnel.
- **`taskkill` sur le processus qui écoute le port 11434**, avant tout le reste.
  Sur un poste où ce port sert à autre chose, le lanceur tuait un programme sans
  rapport avec Épure. `epure_tray.py` cible `ollama.exe` par nom d'image.

---

## Mettre à jour

```bash
git pull
cd backend   && pip install -r requirements.txt
cd ../frontend && npm ci
```

Puis relancez (`python epure_tray.py`, ou les trois services à la main).

Rien n'est automatique au-delà de ces trois lignes. Six points à connaître : les
cinq premiers concernent la mise à jour elle-même, le sixième la façon de
modifier Épure pour que les mises à jour suivantes se passent bien.

### 1. Vos modules installés ne sont pas mis à jour

Installer un module du catalogue **copie** ses trois fichiers vers
`backend/modules/<id>/` et `frontend/src/modules/generated/<id>/` — deux
emplacements ignorés par git. Un `git pull` qui corrige
`modules-catalogue/<id>/` ne touche donc pas votre copie : elle reste à la
version du jour où vous l'avez installée.

Pour récupérer la correction : **Réglages › Catalogue**, supprimer puis
réinstaller. La suppression écrit d'abord une sauvegarde horodatée dans
`backend/modules/_backups/<id>/`.

### 2. `backend/.env` n'est jamais touché

Seul `.env.example` est versionné. Une variable qui y apparaît (nouvelle option,
nouveau fournisseur) doit être recopiée à la main dans votre `.env`. Après un
pull, la différence se lit d'une commande :

```bash
git diff HEAD@{1} -- backend/.env.example
```

### 3. Le frontend doit être reconstruit hors mode dev

`npm ci` efface `node_modules` et le réinstalle exactement d'après
`package-lock.json` — c'est ce qui rend l'installation reproductible, et c'est
pour ça qu'on ne met pas `npm install` ici. Mais il ne construit rien :

| Contexte | Ce qu'il reste à faire |
| --- | --- |
| `npm run dev` (ce que lance `epure_tray.py`) | rien : Vite recompile à chaud |
| Frontend construit (Docker, `npm run build`) | `npm run build`, puis redémarrer |

### 4. Un module du cœur ajouté par la mise à jour reste invisible

`modules_activés`, dans `backend/memory/instance_config.json`, est une liste
**explicite et ordonnée** qui pilote à la fois le montage du routeur et la barre
de modules. Un module que la mise à jour ajoute au cœur n'y figure pas : il sera
installé, mais ni monté ni affiché.

Activez-le dans **Réglages › Modules**. Il n'existe pas de mécanisme de
migration : votre config est fusionnée avec les défauts, donc un champ
nouvellement apparu est comblé, mais rien ne devine qu'une nouvelle entrée doit
rejoindre *votre* liste ordonnée.

(Le cas inverse est déjà couvert : un id resté dans la liste alors que le module
a été supprimé du disque est filtré à la lecture.)

### 5. Les modèles Ollama restent à votre charge

Si `backend/config.yaml` se met à pointer un modèle que vous n'avez pas, rien ne
le signale au démarrage : `/health` répond, l'interface s'affiche, et c'est le
premier message envoyé qui échoue.

```bash
ollama list                      # ce que vous avez déjà
ollama pull <le modèle de config.yaml>
```

### 6. Ne modifiez pas le cœur à la main

C'est le point le plus probable en pratique, et le seul qui dépende de vous
avant la mise à jour. Les fichiers du cœur sont **suivis par git** :

- `backend/core/`, `backend/main.py` ;
- `backend/modules/{_atelier,admin,chat,hello,history,settings}/` ;
- tout `frontend/src/`, à l'exception de `src/modules/generated/`.

Une retouche locale sur l'un d'eux, et `git pull` s'arrête sur un conflit à
résoudre à la main.

Passez par l'**Atelier**. Il écrit dans des chemins que `.gitignore` exclut
(`backend/modules/<votre-id>/`, `frontend/src/modules/generated/<votre-id>/`) :
vos modules survivent aux mises à jour sans jamais entrer en conflit avec elles.
C'est exactement ce à quoi sert l'allowlist de `.gitignore` — le cœur est suivi,
tout le reste est à vous.

---

## Clés API cloud (optionnelles)

Aucune clé n'est nécessaire : sans clé, seuls les modèles Ollama locaux sont
utilisés. Pour activer un fournisseur cloud, renseignez la clé correspondante
dans `backend/.env` (voir `backend/.env.example`) :

| Variable            | Fournisseur | Où obtenir la clé                          |
| ------------------- | ----------- | ------------------------------------------ |
| `GEMINI_API_KEY`    | Google      | <https://aistudio.google.com/apikey>       |
| `GROQ_API_KEY`      | Groq        | <https://console.groq.com/keys>            |
| `CEREBRAS_API_KEY`  | Cerebras    | <https://cloud.cerebras.ai/>               |
| `MISTRAL_API_KEY`   | Mistral AI  | <https://console.mistral.ai/api-keys>      |
| `NVIDIA_API_KEY`    | NVIDIA NIM  | <https://build.nvidia.com/>                |

> `.env` n'est jamais committé (cf. `.gitignore`). Ne versionnez que `.env.example`.

---

## Configuration (variables d'environnement)

Toutes optionnelles ; définies dans `backend/.env` (local) ou via Compose.

| Variable               | Défaut                                        | Rôle                                                          |
| ---------------------- | --------------------------------------------- | ------------------------------------------------------------- |
| `EPURE_FICHES_DIR`     | `<repo>/data/fiches`                          | Dossier racine des fiches PDF (RAG).                          |
| `EPURE_MODELS_DIR`     | `backend/piper_models`                        | Cache du modèle de synthèse vocale (76 Mo, téléchargé).       |
| `EPURE_BIND`           | `127.0.0.1`                                   | Interface d'écoute du backend lancé par `epure_tray.py`.      |
| `EPURE_ALLOWED_HOSTS`  | `localhost,127.0.0.1,::1`                     | En-têtes `Host` acceptés (anti DNS rebinding).                |
| `EPURE_CORS_ORIGINS`   | `http://localhost:5173,http://127.0.0.1:5173` | Origines autorisées (séparées par des virgules).              |
| `EPURE_LOG_LEVEL`      | `INFO`                                        | Niveau de log (`DEBUG`/`INFO`/`WARNING`/`ERROR`).             |
| `OLLAMA_HOST`          | `http://localhost:11434`                      | URL du serveur Ollama (URL complète, jamais `0.0.0.0`).       |

### Ouvrir l'API à d'autres appareils — à lire avant

Par défaut le backend n'écoute que sur la boucle locale : il est injoignable
depuis le réseau. `EPURE_BIND=0.0.0.0` lève cette restriction (accès depuis un
téléphone, un autre poste…), mais **l'API n'est alors protégée que par un seul
token**, et elle expose l'agent de code et l'Atelier — c'est-à-dire l'exécution
de commandes sur la machine. Sur un réseau partagé (wifi d'établissement), c'est
une exposition réelle, pas théorique.

Si vous ouvrez malgré tout, ajoutez le nom d'hôte ou l'IP utilisés pour joindre
Épure à `EPURE_ALLOWED_HOSTS` (ex. `localhost,127.0.0.1,::1,192.168.1.20`) :
sinon toutes les requêtes reçoivent un `400 Invalid host header`. Ce filtre
existe parce que la seule vérification de l'IP source ne suffit pas — une page
web dont le domaine résout vers `127.0.0.1` se présente comme un client local
et pourrait récupérer le token via `/pair`.

Les paramètres de modèle, génération, RAG et voix sont dans
`backend/config.yaml`. Les `watch_folders` y sont des chemins relatifs,
résolus sous `EPURE_FICHES_DIR` (un chemin absolu reste utilisé tel quel).

### Voix — le modèle est téléchargé au premier usage

Le modèle de synthèse Piper (`fr_FR-upmc-medium`, **~77 Mo**) n'est pas dans le
dépôt : c'est un binaire, pas du code, et le versionner faisait payer 71 Mo de
`.git` à chaque clone. Il est récupéré depuis
[`rhasspy/piper-voices`](https://huggingface.co/rhasspy/piper-voices) à la
**première synthèse vocale**, pas au démarrage, puis vérifié par sha256 et rangé
dans `backend/piper_models/` (`EPURE_MODELS_DIR`). Les téléchargements suivants
n'ont pas lieu : le modèle reste sur le disque.

L'interface prévient avant de lancer les 77 Mo et n'insiste pas si vous refusez.
La progression part dans les logs du backend.

**Hors ligne, ou si le téléchargement échoue** : la synthèse vocale est
indisponible et le dit (`503` avec un message explicite) ; **tout le reste
d'Épure fonctionne normalement** — chat, RAG, flashcards, transcription. La voix
est optionnelle par conception. Une tentative ratée n'est pas mise en cache : la
synthèse suivante réessaie, donc il suffit de retrouver du réseau.

Si l'empreinte du fichier téléchargé ne correspond pas à celle attendue, le
fichier est supprimé plutôt que conservé — mieux vaut pas de voix qu'une voix
issue d'un fichier qu'on n'a pas reconnu.

---

## Atelier — moteurs de génération Claude Code

L'Atelier (création/modification de modules) propose 3 moteurs. Leur disponibilité
est diagnostiquée dans **Réglages › Atelier — moteurs** (bouton « Re-tester »).

- **Ollama (local)** — toujours disponible (utilise le modèle actif).
- **Claude Code (abonnement)** `claude_sub` — nécessite le CLI `claude` :
  ```bash
  npm install -g @anthropic-ai/claude-code      # installe le CLI
  claude setup-token                            # auth abonnement (jeton OAuth, headless)
  #   ou, en interactif : lancer `claude` puis /login
  ```
  Le backend détecte `claude` via le PATH ; s'il n'y est pas, renseignez son chemin
  dans **Réglages › Atelier** (champ « Chemin du binaire claude »). Ne définissez
  **pas** `ANTHROPIC_API_KEY` (elle primerait sur l'abonnement).
- **Claude Code (passerelle)** `claude_gateway` — nécessite le CLI `claude` **et**
  une passerelle Anthropic-compatible locale (ex. [LiteLLM](https://docs.litellm.ai/)
  exposant `/v1/messages`, routant vers Ollama/Bedrock/…). Renseignez l'URL + le
  modèle (+ clé éventuelle) dans **Réglages › Atelier**. Le backend pointe
  `ANTHROPIC_BASE_URL` dessus pour les générations.

## Catalogue de modules

Épure est livrée avec un **cœur** (Chat, Admin, Historique, Réglages) et un
**catalogue** de modules installables à la demande, dans `modules-catalogue/` :
Code, Docs, Flashcards, Kholle, Réviseur, Rangement.

L'installation se fait depuis **Réglages › Catalogue**. Elle copie les trois
fichiers du module vers leurs emplacements d'exécution :

```
modules-catalogue/<id>/manifest.json   →  backend/modules/<id>/manifest.json
modules-catalogue/<id>/router.py       →  backend/modules/<id>/router.py
modules-catalogue/<id>/Component.tsx   →  frontend/src/modules/generated/<id>/Component.tsx
```

Rien n'est téléchargé : le catalogue est local, et le code d'un module non
installé n'est simplement pas dans votre bundle.

### ⚠️ Un module installé n'apparaît qu'après reconstruction du frontend

L'installation écrit dans `frontend/src/modules/generated/`. Deux cas :

| Contexte | Effet |
|---|---|
| **Serveur de développement** (`npm run dev`, ce que lance `epure_tray.py`) | Vite détecte le fichier et recharge à chaud — le module apparaît **immédiatement**. |
| **Frontend déjà construit** (image Docker, `npm run build`) | Le bundle est figé : le module n'apparaît **qu'après un `npm run build`** et un redémarrage du conteneur. Le backend, lui, monte sa route tout de suite. |

C'est une limite assumée du choix « catalogue local » plutôt que chargement de
JavaScript distant à l'exécution : exécuter du JS tiers dans l'origine de
l'application annulerait la frontière de sécurité (tout module chargé pourrait
lire le token d'API dans `localStorage`). Le détail du raisonnement est dans
`docs/catalogue-modules.md` §0.

**Suppression** : Réglages › Catalogue › Supprimer. Une sauvegarde horodatée est
écrite dans `backend/modules/_backups/<id>/<horodatage>/` **avant** tout
effacement. Les modules du cœur ne sont pas supprimables.

---

## Tests

```bash
cd backend
python -m unittest discover -s . -p "test_*.py"   # la suite complète (celle de la CI)
python test_web_search.py --live                  # vraie recherche réseau (démo)
```

---

## Dépannage

- **Modèles indisponibles / chat vide** : vérifiez qu'Ollama tourne et qu'un
  modèle est installé (`ollama list`). En Docker, vérifiez `OLLAMA_HOST`.
- **Premier démarrage lent** : téléchargement des modèles ML (mis en cache
  ensuite dans le volume `hf_cache`).
- **Erreurs CORS dans le navigateur** : ajoutez l'origine du frontend à
  `EPURE_CORS_ORIGINS`.
