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
from fastapi.responses import JSONResponse, StreamingResponse
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
from core.orchestrator import OrchestratorEngine
from core.flashcards import FlashcardsEngine
from core.history import HistoryEngine
from core.llm import LLMEngine
from core import module_workshop
from core.instance import instance_config, fiches_root, fiches_watch_paths
from core.memory import MemoryEngine
from core.module_registry import (
    list_modules as _list_modules,
    register_routers as _register_routers,
    set_status as _set_module_status,
)
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

app = FastAPI(title="Épure", version="1.0.0")

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


# ── Gestion d'erreurs : JSON propre pour toute exception non gérée ────────────
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
    }

    def _cloud_dispo(m: dict) -> bool:
        # _disponible: True/False = verdict du /v1/models live ; None = inconnu → clé
        if m.get("_disponible") is False:
            return False
        return key_ok.get(m["provider"], False)

    cloud: dict[str, list] = {}
    for cat, models in catalog.items():
        cloud[cat] = [
            {k: v for k, v in m.items() if not k.startswith("_")} | {"disponible": _cloud_dispo(m)}
            for m in models
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
        recommandations["Conversation instantanée"] = "groq:llama-3.1-8b-instant"

    return {"local": local, "local_npu": local_npu, "cloud": cloud, "recommandations": recommandations}


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
async def workshop_engines():
    """Disponibilité des 3 moteurs (claude_gateway désactivé si injoignable)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, module_workshop.engines_status)


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
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/workshop/{module_id}/approve")
async def workshop_approve(module_id: str):
    """Activation manuelle : backup + déplacement + remontage + modules_activés."""
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, module_workshop.approve, module_id, app)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/workshop/{module_id}/reject")
async def workshop_reject(module_id: str):
    return await asyncio.get_running_loop().run_in_executor(
        None, module_workshop.reject, module_id
    )


@app.websocket("/ws/workshop")
async def ws_workshop(websocket: WebSocket):
    """Stream de génération (ollama / claude headless) + pilotage mode terminal."""
    await websocket.accept()
    loop = asyncio.get_running_loop()

    async def _emit(ev: dict):
        await websocket.send_text(json.dumps(ev, ensure_ascii=False))

    async def _stream_generator(gen):
        """Draine un générateur synchrone (engine) vers le WebSocket."""
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
            await _emit(ev)

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
                    await _emit({"type": "error", "content": str(exc)})
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
                    if engine == "ollama":
                        ollama_model = msg.get("ollama_model") or None
                        gen = module_workshop.generate_ollama(mid, spec, kind, model=ollama_model)
                    else:
                        gen = module_workshop.generate_claude_headless(mid, spec, kind, engine)
                    await _stream_generator(gen)
                    await _validate_and_report(mid)
                    bg_mid = mid
                elif mtype == "terminal_done":
                    mid = msg.get("id", "")
                    await _validate_and_report(mid)
                    bg_mid = mid
            except Exception as exc:
                logger.exception("Atelier ws : traitement du message %s", mtype)
                await _emit({"type": "error", "content": str(exc)})
            finally:
                await _emit({"type": "done"})

            if bg_mid:
                asyncio.create_task(_background_typecheck(bg_mid))

    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    loop = asyncio.get_running_loop()
    models = await loop.run_in_executor(None, get_ollama_installed)
    if models is not None:
        ctx = memory.get_context()
        active_model = ctx.get("modèle_actif", llm._model)
        ollama_ok = True
    else:
        active_model, models, ollama_ok = "", [], False

    try:
        flm_ok = await asyncio.wait_for(
            loop.run_in_executor(None, check_flm), timeout=2.0
        )
    except Exception:
        flm_ok = False

    return {"ollama": ollama_ok, "model": active_model, "models": models, "flm": flm_ok}


# ── Montage des routeurs de modules ─────────────────────────────────────────
# Monte tous les modules actifs disposant d'un modules/<id>/router.py (core ou
# non). Les modules core pas encore migrés restent décorés sur `app` ci-dessus.
_register_routers(app)
