import asyncio
import html as _htmllib
import io
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path
from threading import Thread
from typing import Optional

import pypdf
import yaml
from dotenv import load_dotenv, set_key as dotenv_set_key
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_ENV_FILE = Path(__file__).parent / ".env"

from core.admin import AdminEngine
from core.codeagent import (
    CodeAgent, execute_code as _code_exec, create_file as _code_create,
    read_file as _code_read, delete_path as _code_delete, create_folder as _code_mkdir,
    get_tree as _code_tree, _safe_path as _code_safe_path, SecurityError as _CodeSecurityError,
    WORKSPACE as _CODE_WORKSPACE, install_package as _code_install,
    generate_tests as _code_generate_tests,
)
from core.consolidation import ConsolidationEngine
from core.docanalysis import DocAnalysisEngine
from core.embedding_install import EmbeddingIndisponible
from core.orchestrator import OrchestratorEngine
from core.flashcards import FlashcardsEngine
from core.history import HistoryEngine
from core.llm import LLMEngine
from core import module_workshop
from core.instance import (
    est_modele_cloud as _est_modele_cloud,
    fiches_root, fiches_watch_paths, instance_config,
)
from core.memory import MemoryEngine
from core.module_registry import (
    list_modules as _list_modules,
    migrate_module_state as _migrate_module_state,
    register_routers as _register_routers,
    set_status as _set_module_status,
)
from core.paths import resolve_web_dir
from core.models import (
    ModelsRegistry, RECOMMENDATION_OVERRIDES, FLM_MODELS_STATIC,
    QUALITATIVE_METADATA, check_flm, flm_model_ids, get_flm_installed,
    get_ollama_installed,
)
from core.quota_tracker import QuotaTracker
from core.rag import RAGEngine
from core.voice import PiperEngine, WhisperEngine

# ── Logging uniforme (format + niveau configurable via EPURE_LOG_LEVEL) ──────
_LOG_LEVEL = os.environ.get("EPURE_LOG_LEVEL", "INFO").strip().upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,  # remplace toute config posée par une dépendance importée plus tôt
)
logger = logging.getLogger(__name__)
# Réduit le bruit des bibliothèques tierces très verbeuses.
for _noisy in ("httpx", "watchdog", "sentence_transformers", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Le token ne doit pas atterrir dans un journal (CLAUDE.md §6) ─────────────
# uvicorn journalise le chemin AVEC sa query, et le token du WebSocket voyage en
# query param faute d'en-tête possible sur `new WebSocket()`. À poser ici, juste
# après basicConfig : uvicorn a déjà configuré ses loggers quand il importe
# l'app, donc `uvicorn.access` et `uvicorn.error` existent. Cf. core/logs.py.
from core.logs import masquer_secrets_dans_logs  # noqa: E402

logger.debug("Masquage des secrets dans les logs : %s", masquer_secrets_dans_logs())

app = FastAPI(title="Épure", version="1.0.0")

# ── Auth locale : token d'instance exigé partout sauf /health et /pair ───────
# Enregistré AVANT CORSMiddleware (donc couche plus interne) : les préflights
# OPTIONS sont servis par CORS sans token, et les 401 gardent les en-têtes CORS.
from core.auth import get_api_token, is_local_client, token_ok, ws_require_token

_AUTH_EXEMPT_PATHS = {"/health", "/pair"}

#: Préfixe des assets du frontend construit, exempté d'authentification quand le
#: service statique est monté (cf. :func:`_register_web`). Rempli à
#: l'enregistrement, vide en mode développement.
_WEB_EXEMPT_PATHS: set[str] = set()
_WEB_ASSETS_PREFIX = "/_assets/"


# ── L'Atelier n'est pas livré dans un paquet distribué ───────────────────────
# Lu AU DÉMARRAGE, comme EPURE_ALLOWED_HOSTS et EPURE_CORS_ORIGINS juste en
# dessous : c'est un réglage d'instance, pas une bascule par requête.
#
# « Désactivé » et non « retiré », et ce n'est pas de la paresse : `core/
# catalogue.py` importe sept symboles de `core/module_workshop.py`, et c'est ce
# qui fait marcher `POST /settings/catalogue/{id}/install` et
# `DELETE /settings/modules/{id}` — les deux fonctions que le destinataire du
# paquet garde. `module_workshop` importe lui-même `core.module_validate` au
# niveau module. Retirer ces fichiers du paquet casserait donc l'écran Réglages,
# pas l'Atelier. Ce qu'on retire, ce sont les ROUTES et l'écran ; le code qui
# sert au catalogue reste.
_ATELIER_ACTIF = os.environ.get("EPURE_ATELIER", "1").strip() != "0"

#: Routes de l'Atelier, à refuser quand il est désactivé. `/settings/test/` et
#: `/settings/gateway/` en font partie : ce sont les diagnostics de moteurs de
#: l'écran Réglages › Atelier, inutiles sans lui et qui lancent des process.
_ATELIER_PREFIXES = ("/workshop", "/ws/workshop", "/settings/test/", "/settings/gateway/")

if not _ATELIER_ACTIF:
    logger.info("Atelier DÉSACTIVÉ (EPURE_ATELIER=0) — routes %s refusées", _ATELIER_PREFIXES)


def _atelier_refuse(chemin: str) -> bool:
    """True si ``chemin`` appartient à l'Atelier et que l'Atelier est éteint.

    404 et non 403 : dans un paquet distribué, l'Atelier n'existe pas, et le
    destinataire n'a pas à apprendre qu'il aurait pu exister. C'est aussi ce que
    verra un module généré qui tenterait ces routes.
    """
    if _ATELIER_ACTIF:
        return False
    for prefixe in _ATELIER_PREFIXES:
        if prefixe.endswith("/"):
            if chemin.startswith(prefixe):
                return True
        elif chemin == prefixe or chemin.startswith(prefixe + "/"):
            return True
    return False


def _est_public(chemin: str, methode: str) -> bool:
    """Requête servie sans token d'API.

    Trois familles, et rien d'autre : les deux exemptions historiques
    (``/health``, ``/pair``), les préflights CORS, et — seulement si le frontend
    construit est servi (mode paquet) — les fichiers statiques de l'interface.

    L'exemption statique est nécessaire : la page HTML doit se charger AVANT que
    son JavaScript puisse s'appairer via ``/pair``. Elle est sûre parce qu'aucun
    de ces fichiers ne contient de secret — le token vient de ``/pair``, jamais
    du bundle.

    Deux précautions qui ne sont pas décoratives :

    * les fichiers de la racine de ``dist/`` sont exemptés **un par un, par
      égalité stricte**, pas par préfixe : ``/`` et ``/index.html`` ne doivent
      pas ouvrir tout l'espace de nommage ;
    * un chemin contenant ``..`` est refusé d'office. Starlette ne normalise pas
      le chemin ASGI, donc ``/_assets/../models`` satisfait un test de préfixe
      naïf. Le routeur ne servirait pas ``/models`` pour autant (il n'y a aucune
      route qui corresponde à ce chemin brut), mais faire dépendre une décision
      d'authentification de ce détour serait mal placé.

    Le préfixe ``/_assets/`` est lui-même sûr par construction : un id de module
    valide est ``[a-z][a-z0-9_]{1,30}`` (``module_workshop._ID_RE``), donc aucun
    module ne peut poser de route sous ce préfixe. C'est la raison du réglage
    ``build.assetsDir = '_assets'`` dans ``vite.config.ts`` — avec le défaut
    ``assets``, un module nommé ``assets`` aurait vu ses routes exemptées.
    """
    if methode == "OPTIONS":
        return True
    if ".." in chemin:
        return False
    if chemin in _AUTH_EXEMPT_PATHS or chemin in _WEB_EXEMPT_PATHS:
        return True
    return bool(_WEB_EXEMPT_PATHS) and chemin.startswith(_WEB_ASSETS_PREFIX)


@app.middleware("http")
async def _require_api_token(request: Request, call_next):
    # AVANT le contrôle de token : une route absente de cette installation doit
    # répondre 404 même sans token, sinon le 401 révèle qu'elle existe.
    if _atelier_refuse(request.url.path):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if _est_public(request.url.path, request.method):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    candidate = header[7:].strip() if header.startswith("Bearer ") else ""
    if not token_ok(candidate):
        return JSONResponse(
            status_code=401,
            content={"detail": "Token d'API requis — appairage via /pair sur la machine hôte"},
        )
    return await call_next(request)

# ── CORS explicite : origines via EPURE_CORS_ORIGINS, jamais "*" ──────────────
_DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
_cors_origins = [
    o.strip()
    for o in os.environ.get("EPURE_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if o.strip()
]
logger.info("CORS — origines autorisées : %s", _cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Contrôle de l'en-tête Host : garde-fou anti DNS rebinding ────────────────
# Menace réelle (cf. CLAUDE.md §6) : une page web visitée par l'utilisateur, dont
# le domaine résout vers 127.0.0.1, devient *same-origin* avec l'API. CORS ne
# s'applique alors pas, et `request.client.host` vaut bel et bien « 127.0.0.1 »
# — la garde IP de /pair (exempt d'auth) laisse donc passer, et le token part.
# Le seul élément qui distingue encore cette page du frontend légitime est
# l'en-tête Host, que le navigateur remplit avec le domaine de l'attaquant.
#
# ORDRE — IMPÉRATIF : Starlette empile `user_middleware` dans l'ordre INVERSE de
# l'ajout (`add_middleware` fait `insert(0, ...)`). Ajouté EN DERNIER dans ce
# fichier, ce middleware est donc la couche la PLUS EXTERNE : il filtre avant
# CORS et avant le contrôle de token, y compris sur les chemins exemptés
# (/health, /pair). Le remonter au-dessus du bloc CORS l'enfouirait sous celui-ci
# et rouvrirait la faille. Vérifié par test_auth_surface.MiddlewareOrderTest.
_ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("EPURE_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",")
    if h.strip()
]
logger.info("Hôtes autorisés (en-tête Host) : %s", _ALLOWED_HOSTS)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)


# ── Gestion d'erreurs ────────────────────────────────────────────────────────

@app.exception_handler(EmbeddingIndisponible)
async def _embedding_indisponible_handler(request: Request, exc: EmbeddingIndisponible):
    """503 avec l'état d'avancement, au lieu d'un 500 « ImportError » opaque.

    Un GESTIONNAIRE et non un `try` par endpoint, et c'est le point de ce choix :
    les trois collections vectorielles (`fiches`, `doc_analysis`, `history`)
    partagent UN store, donc tout ce qui les touche passe par le même
    `VectorStore.__init__` — `GET /rag/files` et `POST /files/upload` côté
    Réglages, le chargement de documents, la recherche du chat, l'historique
    vectoriel, les modules du catalogue qui listent les fiches indexées. Les
    traiter un par un garantirait d'en oublier, et un seul oublié rend
    l'interface inexplicable au lieu de simplement dégradée.

    Starlette cherche un gestionnaire en remontant le `__mro__` de l'exception :
    celui-ci gagne donc sur le gestionnaire générique d'`Exception` ci-dessous,
    plus général.

    503 et non 500, pour la même raison que `VoiceModelUnavailable` : ce n'est pas
    une panne d'Épure, c'est une capacité pas encore prête. Et `logger.warning`
    plutôt qu'`exception` — une installation en cours n'est pas un incident dont
    on veut la pile.
    """
    logger.warning("Recherche documentaire indisponible sur %s %s : %s",
                   request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": str(exc), **exc.etat})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Renvoie un JSON uniforme (500) au lieu d'une trace brute.

    Les HTTPException conservent leur traitement dédié (codes/détails voulus).
    """
    logger.exception("Erreur non gérée sur %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Erreur interne du serveur", "type": exc.__class__.__name__},
    )

# Moteurs partagés et helpers transverses : créés une seule fois dans
# core.runtime, injectés ici et dans les routeurs de modules (alias conservés
# pour ne pas toucher au corps des endpoints non encore migrés).
from core.runtime import (
    llm, rag, memory, docanalysis, code_agent,
    flashcards_engine, admin_engine, models_registry, history_engine,
    consolidation_engine, orchestrator, whisper, piper,
    quota_tracker, usage_tracker,
    provider_of as _provider_of,
    pick_reflection_model as _pick_reflection_model,
    apply_fiches_watch as _apply_fiches_watch,
    SSE_HEADERS as _SSE_HEADERS,
    API_KEY_NAMES as _API_KEY_NAMES,
)



# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@app.get("/models")
async def list_models():
    loop = asyncio.get_running_loop()
    ollama_models = await loop.run_in_executor(None, get_ollama_installed)
    if ollama_models is not None:
        local = [
            {
                "id": name, "nom": name, "provider": "ollama", "disponible": True,
                "description": QUALITATIVE_METADATA.get(name, {}).get("description", ""),
            }
            for name in ollama_models
        ]
    else:
        # Serveur Ollama injoignable → modèle configuré affiché mais indisponible
        local = [{"id": llm._model, "nom": llm._model, "provider": "ollama", "disponible": False}]

    catalog = await models_registry.get_catalog()

    # FLM: server reachable + model physically installed in ~/.flm/models
    try:
        flm_ok = await asyncio.wait_for(
            loop.run_in_executor(None, check_flm), timeout=2.5
        )
    except Exception:
        flm_ok = False
    flm_installed: set[str] = set()
    flm_live: Optional[set[str]] = None
    if flm_ok:
        try:
            flm_installed = await loop.run_in_executor(None, get_flm_installed)
            flm_live = await loop.run_in_executor(None, flm_model_ids)
        except Exception:
            logger.exception("Erreur détection modèles FLM installés")

    local_npu = []
    for m in FLM_MODELS_STATIC:
        mid = m["id"].split("flm:", 1)[1]
        dispo = (
            flm_ok
            and mid in flm_installed
            and (flm_live is None or mid in flm_live)
        )
        local_npu.append(
            {k: v for k, v in m.items() if not k.startswith("_")} | {"disponible": dispo}
        )

    key_ok: dict[str, bool] = {
        "gemini":   bool(os.environ.get("GEMINI_API_KEY", "").strip()),
        "groq":     bool(os.environ.get("GROQ_API_KEY", "").strip()),
        "cerebras": bool(os.environ.get("CEREBRAS_API_KEY", "").strip()),
        "mistral":  bool(os.environ.get("MISTRAL_API_KEY", "").strip()),
        "nvidia":   bool(os.environ.get("NVIDIA_API_KEY", "").strip()),
        "deepseek": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
    }

    def _cloud_dispo(m: dict) -> bool:
        # _disponible: True/False = verdict du /v1/models live ; None = inconnu → clé
        if m.get("_disponible") is False:
            return False
        return key_ok.get(m["provider"], False)

    # Un fournisseur sans clé ne rend AUCUN modèle : ses modèles ne sont pas
    # listés du tout, au lieu d'être listés grisés. Sans clé, l'entrée n'apprend
    # rien à qui n'a pas l'intention d'en poser une, et elle occupait l'essentiel
    # du sélecteur du chat — six fournisseurs de modèles morts au-dessus des
    # modèles locaux qui, eux, marchent. Même parti pris que le catalogue de
    # modules non livré (`GET /settings/catalogue` renvoie une liste vide et le
    # bouton n'apparaît pas) : l'incapacité est silencieuse plutôt qu'affichée.
    #
    # Ce qui RESTE listé et grisé : un modèle dont la clé est bien là mais que le
    # /v1/models du fournisseur ne connaît plus (`_disponible is False`). Celui-là
    # est un diagnostic — ID retiré du catalogue amont — et se voit dans Réglages.
    # `disponible: False` sur un modèle cloud a donc désormais UNE seule cause.
    #
    # Les clés se posent dans Réglages, qui lit `GET /settings/api-keys` et
    # `/settings/provider-models` : cet écran ne dépend pas de la liste ci-dessous
    # et continue de montrer les six fournisseurs, avec ou sans clé.
    cloud: dict[str, list] = {}
    for cat, models in catalog.items():
        cloud[cat] = [
            {k: v for k, v in m.items() if not k.startswith("_")} | {"disponible": _cloud_dispo(m)}
            for m in models
            if key_ok.get(m["provider"], False)
        ]

    # Recommendations: first available model per usage (based on _usages metadata)
    recommandations: dict[str, str] = {}
    for models in catalog.values():
        for m in models:
            if not _cloud_dispo(m):
                continue
            for usage in m.get("_usages", []):
                if usage not in recommandations:
                    recommandations[usage] = m["id"]

    # Apply static overrides only when the target model exists in the live
    # catalog and is available (avoids recommending a deprecated model ID)
    available_ids = {
        m["id"] for models in catalog.values() for m in models if _cloud_dispo(m)
    }
    for usage, model_id in RECOMMENDATION_OVERRIDES.items():
        if model_id in available_ids:
            recommandations[usage] = model_id

    # "Conversation instantanée" : FLM first (si installé), fallback to Groq
    if flm_ok and "qwen3:4b" in flm_installed:
        recommandations["Conversation instantanée"] = "flm:qwen3:4b"
    elif key_ok.get("groq", False):
        # `groq:llama-3.1-8b-instant` jusqu'au 2026-08-24 : 404 mesure.
        recommandations["Conversation instantanée"] = "groq:openai/gpt-oss-20b"

    # `fournisseurs` : quelles clés sont posées. Le frontend en a besoin pour ses
    # recommandations curées (ModuleBar.MODULE_RECOMMENDATIONS), qui nomment des
    # IDs en dur : sans cette carte, un modèle absent de `cloud` parce que sa clé
    # manque serait indistinguable d'un modèle absent du catalogue amont, et
    # l'interface le proposerait comme « inconnu, tentons » — un clic vers une
    # erreur. Ne porte aucun secret : six booléens, pas les clés.
    return {
        "local": local,
        "local_npu": local_npu,
        "cloud": cloud,
        "fournisseurs": key_ok,
        "recommandations": recommandations,
    }


# ---------------------------------------------------------------------------
# Instance config & modules
# ---------------------------------------------------------------------------

@app.get("/instance/config")
async def instance_config_get():
    return instance_config.get()


@app.put("/instance/config")
async def instance_config_put(request: Request):
    """Merge partiel de la config d'instance + effets de bord côté serveur."""
    partial = await request.json()
    if not isinstance(partial, dict):
        raise HTTPException(status_code=400, detail="Corps JSON attendu (objet)")

    # `providers.local` pilote TOUTES les tâches de fond (résumés, titrage,
    # classification, réflexion de l'agent de code). Y écrire un identifiant
    # cloud viderait la règle « pas de cloud sans choix explicite » de son sens,
    # tout en ayant l'air d'un réglage valide — on refuse à la source plutôt que
    # de le corriger en silence à la lecture (`modele_local_defaut` sait déjà
    # l'ignorer, mais un réglage qu'on ignore est un réglage qui ment).
    if isinstance(partial.get("providers"), dict):
        local = (partial["providers"].get("local") or "").strip()
        if local and _est_modele_cloud(local):
            raise HTTPException(
                status_code=400,
                detail=(f"« {local} » est un modèle cloud. Le modèle des tâches de "
                        "fond doit être local (Ollama, ou flm: pour le NPU) : ces "
                        "tâches partent sans que vous les demandiez."),
            )

    cfg = instance_config.update(partial)

    # Le modèle actif est aussi stocké dans le contexte mémoire (source utilisée
    # par le chat/orchestrateur) : on le synchronise quand il change.
    if "providers" in partial and isinstance(partial["providers"], dict):
        actif = partial["providers"].get("actif")
        if actif:
            memory.update_context(**{"modèle_actif": actif})

    # Nouveaux dossiers de fiches → on les met sous surveillance (les retraits
    # prennent effet au prochain démarrage : watchdog ne propose pas d'unwatch).
    if "fiches" in partial:
        _apply_fiches_watch()

    return cfg


@app.get("/modules")
async def modules_list():
    return {"modules": _list_modules()}


class ModuleStatusRequest(BaseModel):
    status: str


@app.put("/modules/{module_id}/status")
async def module_set_status(module_id: str, req: ModuleStatusRequest):
    updated = _set_module_status(module_id, req.status)
    if updated is None:
        raise HTTPException(
            status_code=400,
            detail="Module inconnu, status invalide (active|disabled), ou action interdite",
        )
    return updated


# ---------------------------------------------------------------------------
# Workshop / Atelier de modules
# ---------------------------------------------------------------------------

@app.get("/workshop/engines")
async def workshop_engines(force: bool = False):
    """Disponibilité des moteurs (résultat mis en cache ; force=true → re-test)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, module_workshop.engines_status, force)


@app.get("/workshop/modules")
async def workshop_modules():
    """Tous les modules (pour la liste « modifier »), avec is_core/staging."""
    mods = _list_modules()
    staging = {m["id"]: m for m in module_workshop.list_staging()}
    for m in mods:
        m["staging"] = staging.get(m["id"])
    return {"modules": mods, "staging": list(staging.values())}


class WorkshopGenerateRequest(BaseModel):
    id: str
    engine: str = "ollama"
    mode: str = "headless"


class WorkshopEditRequest(BaseModel):
    engine: str = "ollama"
    mode: str = "headless"


@app.post("/workshop/generate")
async def workshop_generate(req: WorkshopGenerateRequest):
    """Création : prépare le staging d'un NOUVEAU module (génération via /ws/workshop)."""
    try:
        meta = await asyncio.get_running_loop().run_in_executor(
            None, module_workshop.prepare, req.id, "new", req.engine, req.mode
        )
    except module_workshop.SessionLockedError as exc:
        # 409 : session terminale vivante. Récupérable côté front (re-scan), pas
        # un échec de validation — on ne l'écrase donc PAS via le 400 générique.
        raise HTTPException(status_code=409, detail=str(exc))
    except (ValueError, module_workshop.SecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return meta


@app.post("/workshop/{module_id}/edit")
async def workshop_edit(module_id: str, req: WorkshopEditRequest):
    """Modification : copie le module actif dans le staging (génération via /ws/workshop)."""
    try:
        meta = await asyncio.get_running_loop().run_in_executor(
            None, module_workshop.prepare, module_id, "edit", req.engine, req.mode
        )
    except module_workshop.SessionLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ValueError, module_workshop.SecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return meta


@app.get("/workshop/staging/{module_id}")
async def workshop_staging_get(module_id: str):
    """Les 3 fichiers stagés + diff vs actif (si édition)."""
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, module_workshop.read_staging, module_id
        )
    except module_workshop.SecurityError as exc:
        # 400 et pas 404 : l'id est malformé, ce n'est pas « pas encore là ».
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/workshop/{module_id}/validate")
async def workshop_validate(module_id: str):
    """Re-valide un staging existant (sans régénérer) et renvoie le rapport.

    Permet de reprendre un brouillon sauvegardé (ex. après F5) et de réactiver le
    bouton « Approuver » si le code est valide.
    """
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, module_workshop.validate_staging, module_id, False
        )
    except module_workshop.SecurityError as exc:
        # Avant le `except Exception` générique, sinon un id invalide ressort en
        # 404 « staging introuvable » — message trompeur pour un refus de sécurité.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/workshop/{module_id}/approve")
async def workshop_approve(module_id: str, force: bool = False):
    """Activation manuelle : backup + déplacement + remontage + modules_activés.

    force=true active malgré une validation échouée (choix explicite de l'utilisateur).
    """
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, module_workshop.approve, module_id, app, force)
    except (ValueError, module_workshop.SecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/workshop/{module_id}/reject")
async def workshop_reject(module_id: str):
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, module_workshop.reject, module_id
        )
    except module_workshop.SecurityError as exc:
        # reject fait un rmtree : c'est la route où un id non validé coûtait le
        # plus cher (`../hello` supprimait le module en place).
        raise HTTPException(status_code=400, detail=str(exc))


@app.websocket("/ws/workshop")
async def ws_workshop(websocket: WebSocket):
    """Stream de génération (ollama / claude headless) + pilotage mode terminal."""
    # Le middleware HTTP ne voit pas les WebSocket (CLAUDE.md §3.6) : la garde de
    # `_atelier_refuse` ne s'applique pas ici, il faut la reposer. Fermer AVANT
    # `accept()`, comme le contrôle de token juste en dessous.
    if _atelier_refuse("/ws/workshop"):
        await websocket.close(code=1008)  # policy violation
        return
    if not await ws_require_token(websocket):
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()

    async def _emit(ev: dict):
        await websocket.send_text(json.dumps(ev, ensure_ascii=False))

    async def _emit_error(exc: Exception):
        """Erreur typée : le front lit `content`, mais `code` permet de distinguer
        un refus de sécurité (id de module malformé) d'un échec de génération.

        Sans ça un `{"type":"generate","id":"../chat"}` ressortait dans la même
        bannière rouge qu'un timeout Ollama — impossible de voir que la requête
        elle-même avait été refusée.
        """
        code = "invalid_id" if isinstance(exc, module_workshop.SecurityError) else "error"
        await _emit({"type": "error", "code": code, "content": str(exc)})

    async def _stream_generator(gen):
        """Draine un générateur synchrone (engine) vers le WebSocket.
        Retourne {"paused": bool} : True si l'engine a émis un événement 'paused'
        (timeout aider) → on NE valide PAS (le travail est conservé pour reprise)."""
        state = {"paused": False}
        queue: asyncio.Queue = asyncio.Queue()

        def _worker():
            try:
                for ev in gen:
                    asyncio.run_coroutine_threadsafe(queue.put(ev), loop)
            except Exception as exc:
                logger.exception("Erreur génération atelier")
                asyncio.run_coroutine_threadsafe(queue.put({"type": "error", "content": str(exc)}), loop)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        Thread(target=_worker, daemon=True).start()
        while True:
            ev = await queue.get()
            if ev is None:
                break
            if ev.get("type") == "paused":
                state["paused"] = True
            await _emit(ev)
        return state

    async def _validate_and_report(mid: str):
        """Gate RAPIDE (sans tsc) : la revue doit s'afficher immédiatement."""
        await _emit({"type": "validating"})
        res = await loop.run_in_executor(None, module_workshop.validate_staging, mid, False)
        await _emit({"type": "validated", "status": res["status"], "report": res["report"]})

    async def _background_typecheck(mid: str):
        """tsc en tâche de fond — n'a JAMAIS bloqué l'apparition de la revue."""
        try:
            tc = await loop.run_in_executor(None, module_workshop.typecheck_staging, mid)
            warns = tc.get("warnings", [])
            if warns:
                await _emit({"type": "typecheck", "report": {"warnings": warns}})
        except Exception:
            logger.exception("Type-check atelier (tâche de fond) %s", mid)

    _SMOKE_REPAIR_MAX = 2

    async def _background_smoke(mid: str, gen_msg: dict):
        """Smoke test du router stagé (sous-processus isolé) en tâche de fond,
        même pattern que _background_typecheck : la revue s'affiche d'abord,
        le résultat arrive en {type:"smoke"}. En cas d'échec, le traceback est
        renvoyé au moteur de génération actif (même canal que le feedback du
        validateur) pour une passe de correction — max _SMOKE_REPAIR_MAX —
        puis le verdict final (ok / repaired / failed) est émis et persisté
        en meta. Ne tourne qu'après un gate AST réussi ; jamais de réparation
        auto en mode terminal (session pilotée par l'utilisateur)."""
        try:
            meta = module_workshop._read_meta(mid) or {}
            if not ((meta.get("report") or {}).get("ok")):
                return  # gate AST échoué : la revue montre déjà les erreurs
            engine = gen_msg.get("engine") or meta.get("engine") or "ollama"
            kind = gen_msg.get("kind") or meta.get("kind") or "new"
            spec = gen_msg.get("description") or gen_msg.get("message") or meta.get("spec") or ""
            aider_meta = meta.get("aider") or {}
            model = gen_msg.get("ollama_model") or gen_msg.get("model") or aider_meta.get("model") or None
            architect = bool(gen_msg.get("aider_architect") or gen_msg.get("architect")
                             or aider_meta.get("architect"))
            can_repair = (engine in ("ollama", "aider", "claude_sub", "claude_gateway")
                          and (gen_msg.get("mode") or meta.get("mode")) != "terminal")
            if spec:
                await loop.run_in_executor(None, module_workshop.remember_spec, mid, spec)

            await _emit({"type": "smoke", "phase": "running"})
            res = await loop.run_in_executor(None, module_workshop.smoke_test_staging, mid)
            attempts = 0
            while not res.get("ok") and can_repair and attempts < _SMOKE_REPAIR_MAX:
                attempts += 1
                fb = module_workshop.smoke_feedback(res)
                await _emit({"type": "smoke", "phase": "repairing",
                             "attempt": attempts, "max": _SMOKE_REPAIR_MAX, "traceback": fb})
                if engine == "ollama":
                    gen = module_workshop.generate_ollama(mid, spec, kind, model=model, feedback=fb)
                elif engine == "aider":
                    gen = module_workshop.aider_converse(
                        mid, fb, mode="build", restore=False,
                        model=model, architect=architect, kind=kind,
                    )
                else:
                    eff = f"{spec}\n\n[Corrige ces erreurs d'exécution]\n{fb}"
                    gen = module_workshop.generate_claude_headless(mid, eff, kind, engine)
                st = await _stream_generator(gen)
                if st["paused"]:
                    break  # timeout aider : travail conservé, l'utilisateur reprendra
                # La réparation a réécrit le staging : re-gate AST puis re-smoke.
                vres = await loop.run_in_executor(None, module_workshop.validate_staging, mid, False)
                await _emit({"type": "validated", "status": vres["status"], "report": vres["report"]})
                if not vres["report"]["ok"]:
                    res = {"ok": False, "tested": [], "failures": [], "skipped": [],
                           "error": ("La passe de réparation a produit un code rejeté par le "
                                     "validateur :\n" + "\n".join(vres["report"]["errors"]))}
                    continue
                res = await loop.run_in_executor(None, module_workshop.smoke_test_staging, mid)
            status = "ok" if res.get("ok") and attempts == 0 else ("repaired" if res.get("ok") else "failed")
            final = {"status": status, "attempts": attempts,
                     **{k: res.get(k) for k in ("ok", "tested", "failures", "skipped", "error")}}
            await loop.run_in_executor(None, module_workshop.record_smoke, mid, final)
            await _emit({"type": "smoke", "phase": "done", **final})
        except Exception:
            logger.exception("Smoke test atelier (tâche de fond) %s", mid)

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            mtype = msg.get("type")

            # Mode terminal : on ouvre la session et on attend "terminal_done"
            # (surtout PAS de "done" ici, sinon le front fermerait la socket).
            if (mtype == "generate"
                    and msg.get("engine") in ("claude_sub", "claude_gateway")
                    and msg.get("mode") == "terminal"):
                try:
                    info = await loop.run_in_executor(
                        None, module_workshop.open_terminal,
                        msg.get("id", ""), msg.get("description", ""),
                        msg.get("kind", "new"), msg.get("engine"),
                    )
                    await _emit({"type": "terminal_opened", **info})
                except Exception as exc:
                    logger.exception("Ouverture terminal atelier")
                    await _emit_error(exc)
                    await _emit({"type": "done"})
                continue

            # Messages produisant une revue : on émet TOUJOURS "done" (finally),
            # et "error" sur exception ; le tsc part en tâche de fond après "done".
            bg_mid = None
            try:
                if mtype == "generate":
                    mid = msg.get("id", "")
                    kind = msg.get("kind", "new")
                    spec = msg.get("description", "")
                    engine = msg.get("engine", "ollama")
                    # Tolère l'ancien champ `model` (bundle frontend non reconstruit)
                    # autant que le nouveau `ollama_model` — sinon le modèle choisi
                    # est ignoré et la génération retombe sur providers.actif (souvent
                    # un modèle cloud), produisant un module invalide → approbation
                    # impossible.
                    ollama_model = msg.get("ollama_model") or msg.get("model") or None
                    feedback = msg.get("feedback") or None
                    if engine == "ollama":
                        gen = module_workshop.generate_ollama(mid, spec, kind, model=ollama_model, feedback=feedback)
                    elif engine == "aider":
                        architect = bool(msg.get("aider_architect", False))
                        gen = module_workshop.generate_aider_headless(mid, spec, kind, model=ollama_model, architect=architect)
                    else:
                        # claude_* : pas de sélection de modèle ; on intègre le
                        # feedback d'erreur dans la description.
                        eff_spec = spec if not feedback else f"{spec}\n\n[Corrige ces erreurs]\n{feedback}"
                        gen = module_workshop.generate_claude_headless(mid, eff_spec, kind, engine)
                    st = await _stream_generator(gen)
                    if not st["paused"]:
                        await _validate_and_report(mid)
                        bg_mid = mid
                elif mtype == "resume":
                    mid = msg.get("id", "")
                    st = await _stream_generator(module_workshop.resume_aider_headless(mid))
                    if not st["paused"]:
                        await _validate_and_report(mid)
                        bg_mid = mid
                elif mtype == "workshop_chat":
                    mid = msg.get("id", "")
                    mode = msg.get("mode", "plan")
                    fresh = bool(msg.get("fresh", False))
                    sdir = module_workshop._staging_dir(mid)
                    hist_exists = (sdir / ".aider.chat.history.md").is_file()
                    # build : toujours déterministe (jamais de restore) ; plan : reprend
                    # l'historique de la conversation en cours (sauf 1er tour fresh).
                    restore = (mode == "plan") and hist_exists and not fresh
                    gen = module_workshop.aider_converse(
                        mid, msg.get("message", ""),
                        mode=mode, restore=restore, fresh=fresh,
                        model=msg.get("model") or None,
                        architect=bool(msg.get("architect", False)),
                        kind=msg.get("kind", "new"),
                        extra_reads=msg.get("extra_reads") or None,
                    )
                    st = await _stream_generator(gen)
                    if mode == "build" and not st["paused"]:
                        await _validate_and_report(mid)
                        bg_mid = mid
                elif mtype == "grant_read":
                    mid, path = msg.get("id", ""), (msg.get("path", "") or "").strip()
                    ok = module_workshop.grant_read(mid, path)
                    await _emit({"type": "read_granted", "ok": ok, "path": path})
                elif mtype == "terminal_done":
                    mid = msg.get("id", "")
                    await _validate_and_report(mid)
                    bg_mid = mid
            except Exception as exc:
                logger.exception("Atelier ws : traitement du message %s", mtype)
                await _emit_error(exc)
            finally:
                await _emit({"type": "done"})

            if bg_mid:
                asyncio.create_task(_background_typecheck(bg_mid))
                # msg passé tel quel : _background_smoke y lit engine/spec/model
                # quand ils existent (generate, workshop_chat) et retombe sur la
                # meta du staging sinon (resume, terminal_done).
                asyncio.create_task(_background_smoke(bg_mid, msg))

    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

@app.get("/pair")
async def pair(request: Request):
    """Appairage local : renvoie le token d'API, uniquement à la machine hôte.

    Le frontend appelle cette route au premier chargement ; servie depuis
    127.0.0.1 (cas nominal), l'appairage est automatique et invisible. Depuis
    une autre machine → 403 : l'utilisateur colle le code affiché par
    http://localhost:8000/pair ouvert sur le poste qui héberge Épure.
    """
    host = request.client.host if request.client else ""
    if not is_local_client(host):
        raise HTTPException(
            status_code=403,
            detail="Appairage réservé à la machine hôte (ouvrez /pair sur celle-ci)",
        )
    return {"token": get_api_token()}


@app.get("/health")
async def health():
    loop = asyncio.get_running_loop()
    # Bornée comme check_flm juste en dessous. Sans ce wait_for, c'est la sonde
    # Ollama qui dictait la latence de /health : mesuré à 2,05-2,20 s Ollama
    # allumé, et au-delà de 5 s Ollama ÉTEINT — `localhost` résout en ::1 puis
    # 127.0.0.1, et urllib paie son timeout=3 sur chaque famille d'adresse.
    # Or le HEALTHCHECK du Dockerfile a un --timeout=5s : un conteneur dont
    # l'hôte n'a pas d'Ollama passait « unhealthy » au bout de 5 essais alors
    # que l'API répondait parfaitement. Un `depends_on: service_healthy` ne se
    # déclenchait alors jamais — même symptôme que le /openapi.json d'avant,
    # pour une autre cause.
    #
    # wait_for n'interrompt PAS le thread de l'exécuteur (on n'annule pas un
    # thread) : il cesse de l'attendre. La requête sous-jacente se termine dans
    # son coin et /health répond sans elle, en dégradant `ollama` à false. C'est
    # le compromis déjà retenu pour check_flm.
    #
    # Les deux sondes tournent EN PARALLÈLE, et c'est ce qui rend la borne utile.
    # Enchaînées, elles additionnaient leurs plafonds : mesuré à 4,05 s avec les
    # deux serveurs éteints, soit toujours au-dessus du --timeout=5s une fois la
    # latence réseau du conteneur ajoutée. Elles sont indépendantes, rien ne
    # justifie de les séquencer. Plafond réel : max(2, 2) et non 2 + 2.
    async def _borne(sonde, defaut, timeout=2.0):
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, sonde), timeout=timeout
            )
        except Exception:
            return defaut

    models, flm_ok = await asyncio.gather(
        _borne(get_ollama_installed, None),
        _borne(check_flm, False),
    )

    if models is not None:
        ctx = memory.get_context()
        active_model = ctx.get("modèle_actif", llm._model)
        ollama_ok = True
    else:
        active_model, models, ollama_ok = "", [], False

    return {"ollama": ollama_ok, "model": active_model, "models": models, "flm": flm_ok}


# ── Montage des routeurs de modules ─────────────────────────────────────────
# La migration passe AVANT le montage : c'est elle qui purge les fantômes et
# rattrape les modules installés jamais vus, et `register_routers` lit la liste
# qu'elle vient d'écrire. Idempotente — au démarrage suivant elle n'écrit rien.
_migrate_module_state()
# Monte tous les modules actifs disposant d'un modules/<id>/router.py (core ou
# non). Les modules core pas encore migrés restent décorés sur `app` ci-dessus.
_register_routers(app)


# ── Interface servie par FastAPI (paquet distribué) ──────────────────────────

def _servir_fichier(fichier: Path):
    """Fabrique un endpoint qui renvoie un fichier fixe.

    Une fabrique et non une route paramétrée (``/{nom}``) : un paramètre de
    chemin venant du client rouvrirait la question du confinement, pour servir
    trois fichiers connus au démarrage. Ici il n'y a rien à confiner — le chemin
    est capturé à l'enregistrement, le client ne choisit que parmi les routes qui
    existent.
    """

    async def _endpoint() -> FileResponse:
        return FileResponse(fichier)

    return _endpoint


def _register_web(app) -> dict:
    """Sert ``frontend/dist`` — un seul processus au lieu de Vite + uvicorn.

    Éteint et sans effet si le dossier n'a pas d'``index.html`` : c'est le mode
    développement, où Vite sert l'interface sur :5173 et où ce montage n'aurait
    aucun intérêt. Le dossier vient de ``core.paths.resolve_web_dir`` (donc
    surchargeable par ``$EPURE_WEB_DIR``, ce qui rend l'ensemble testable).

    **IMPÉRATIF — pas de catch-all, et surtout pas un mount sur ``/``.** Deux
    raisons, dans cet ordre d'importance :

    1. ``module_workshop._remount`` fait un ``app.include_router`` qui **ajoute
       en fin** de ``app.router.routes``. Starlette prend la première route qui
       correspond : un catch-all posé au démarrage serait donc devant les routes
       de tout module installé **ensuite**, et ``index.html`` répondrait à la
       place du module. Or installer un module depuis le catalogue est
       précisément la fonction que le destinataire du paquet garde
       (``POST /settings/catalogue/{id}/install``).
    2. Il n'y en a pas besoin. Le frontend n'a aucun routeur client (pas de
       ``react-router``, pas de ``history.pushState``) : la navigation est l'état
       React ``activeModule`` persisté dans localStorage. Il n'existe qu'une
       seule URL, ``/``. C'est mesuré dans ``docs/distribution-empaquetee.md``
       §0, et c'est ce qui permet de s'en tenir à des routes explicites.

    Conséquence à garder : une route d'API mal orthographiée répond 404 et non
    ``index.html``, ce qui est le bon comportement et non une limite.
    """
    web = resolve_web_dir()
    index = web / "index.html"
    if not index.is_file():
        logger.info(
            "Interface non servie par le backend (pas de %s) — mode développement",
            index,
        )
        return {"servi": False, "dossier": str(web), "routes": []}

    assets = web / _WEB_ASSETS_PREFIX.strip("/")
    if assets.is_dir():
        app.mount(_WEB_ASSETS_PREFIX.rstrip("/"), StaticFiles(directory=assets), name="web-assets")

    routes = ["/"]
    app.get("/", include_in_schema=False)(_servir_fichier(index))
    _WEB_EXEMPT_PATHS.add("/")
    for fichier in sorted(web.iterdir()):
        if not fichier.is_file():
            continue
        chemin = f"/{fichier.name}"
        app.get(chemin, include_in_schema=False)(_servir_fichier(fichier))
        _WEB_EXEMPT_PATHS.add(chemin)
        routes.append(chemin)

    logger.info(
        "Interface servie depuis %s — %d route(s) racine + %s",
        web, len(routes), _WEB_ASSETS_PREFIX,
    )
    return {"servi": True, "dossier": str(web), "routes": routes}


_register_web(app)
