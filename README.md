# Épure

Assistant d'étude local-first pour la prépa scientifique : chat multi-modèles
(Ollama local + fournisseurs cloud optionnels), analyse documentaire (RAG sur
vos fiches PDF), flashcards, kholles, agent de code et synthèse/transcription
vocale.

- **Backend** : FastAPI (`backend/`)
- **Frontend** : React + Vite + TypeScript (`frontend/`)
- **LLM local** : [Ollama](https://ollama.com) (cloud optionnel : Gemini, Groq, Cerebras, Mistral, NVIDIA)

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
Whisper, Piper) ; comptez quelques minutes. Le healthcheck patiente jusqu'à
5 min avant de considérer le service en échec.

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

`start.ps1` démarre Ollama, le backend et le frontend puis ouvre le navigateur
(adaptez les chemins à votre installation).

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
