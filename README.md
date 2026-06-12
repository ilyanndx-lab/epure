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

| Variable             | Défaut                                            | Rôle                                                        |
| -------------------- | ------------------------------------------------- | ----------------------------------------------------------- |
| `EPURE_FICHES_DIR`   | `<repo>/data/fiches`                              | Dossier racine des fiches PDF (RAG).                        |
| `EPURE_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173`     | Origines autorisées (séparées par des virgules).            |
| `EPURE_LOG_LEVEL`    | `INFO`                                            | Niveau de log (`DEBUG`/`INFO`/`WARNING`/`ERROR`).           |
| `OLLAMA_HOST`        | `http://localhost:11434`                          | URL du serveur Ollama (URL complète, jamais `0.0.0.0`).     |

Les paramètres de modèle, génération, RAG et voix sont dans
`backend/config.yaml`. Les `watch_folders` y sont des chemins relatifs,
résolus sous `EPURE_FICHES_DIR` (un chemin absolu reste utilisé tel quel).

---

## Tests

```bash
cd backend
python -m unittest test_web_search          # tests offline (HTTP mocké)
python test_web_search.py --live            # vraie recherche réseau (démo)
```

---

## Dépannage

- **Modèles indisponibles / chat vide** : vérifiez qu'Ollama tourne et qu'un
  modèle est installé (`ollama list`). En Docker, vérifiez `OLLAMA_HOST`.
- **Premier démarrage lent** : téléchargement des modèles ML (mis en cache
  ensuite dans le volume `hf_cache`).
- **Erreurs CORS dans le navigateur** : ajoutez l'origine du frontend à
  `EPURE_CORS_ORIGINS`.
