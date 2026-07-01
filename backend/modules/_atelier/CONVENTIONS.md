# Conventions de module Épure (à lire avant de générer/éditer)

Un module vit dans `backend/modules/<id>/` (router.py + manifest.json) et
`frontend/src/modules/generated/<id>/Component.tsx`. EXACTEMENT 3 fichiers.

## Accès au LLM (IMPÉRATIF — ne jamais importer un moteur tiers en dur)
Pour appeler un modèle (DeepSeek cloud, Ollama local, ou NPU/flm), utilise le
moteur PARTAGÉ d'Épure, jamais `import flm`/`import ollama` ni de chemin en dur :

    from core.runtime import llm
    messages = [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
    texte = "".join(t for t in llm.stream(messages, model=model) if isinstance(t, str))

`model` est un id Épure « provider:model_id », ex. :
  - "deepseek:deepseek-v4-pro"  (cloud, via DEEPSEEK_API_KEY déjà configurée)
  - "ollama:qwen2.5-coder:7b"   (local)
  - "flm:<modèle>"              (NPU)
  - None / "" → modèle actif de l'instance (providers.actif).
Le routage (clé API, base_url) est géré par LLMEngine. Ne touche JAMAIS aux clés.

## Routes (IMPÉRATIF)
Préfixe TOUTES les routes par l'id du module : `/<id>/...`. Sinon collision avec
les routes core (ex. `/models`, `/analyze` existent déjà au niveau racine).
    router = APIRouter()
    @router.post("/<id>/analyze")
    async def analyze(...): ...

## Interdits (validateur AST — sinon module rejeté)
import subprocess/socket/importlib/ctypes/multiprocessing ; os.system/os.popen/
os.exec* (même via alias `import os as o` ou `getattr(os, "system")`) ;
eval/exec/compile/__import__ ; accès aux variables KEY/TOKEN/SECRET ; accès à
os.environ par clé non littérale (concaténation, variable). RÉSEAU : pas
d'import urllib/http.client/requests/httpx/aiohttp — tout accès réseau/LLM passe
par core.runtime. Chaque route DOIT être préfixée par /<id> (prefix="" au mount).

## Frontend Component.tsx
Composant React par défaut. Imports : `../../../components/ui` (UI partagée),
`../../registry` (SharedModuleProps). Appelle le backend via le client
centralisé `../../../api` — JAMAIS d'URL en dur ni de fetch() nu (l'API exige
un token d'instance, joint automatiquement par apiFetch) :

    import { API, apiFetch } from '../../../api'
    const res = await apiFetch(`${API}/<id>/...`, { method: 'POST', ... })

WebSocket éventuel : `import { wsUrl } from '../../../api'` puis
`new WebSocket(wsUrl('/ws/<id>'))` (appelé au moment de la connexion).
Interdits : dangerouslySetInnerHTML, eval.

## Exemple de référence
Le module `hello` (manifest.json + router.py) t'est fourni en lecture (--read).
