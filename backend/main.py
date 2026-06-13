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
# Voice
# ---------------------------------------------------------------------------

class SynthesizeRequest(BaseModel):
    text: str
    voice: str = "fr_FR-upmc-medium"


@app.post("/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(None, whisper.transcribe, audio_bytes)
    except Exception:
        logger.exception("Erreur transcription /voice/transcribe")
        raise HTTPException(status_code=500, detail="Erreur transcription")
    return {"text": text}


@app.post("/voice/synthesize")
async def voice_synthesize(req: SynthesizeRequest):
    loop = asyncio.get_running_loop()
    try:
        wav_bytes = await loop.run_in_executor(None, piper.synthesize, req.text)
    except Exception:
        logger.exception("Erreur synthèse /voice/synthesize")
        raise HTTPException(status_code=500, detail="Erreur synthèse vocale")
    return StreamingResponse(io.BytesIO(wav_bytes), media_type="audio/wav")


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
# Quota / Usage
# ---------------------------------------------------------------------------

@app.get("/quota/usage")
async def quota_usage():
    return usage_tracker.get_usage()


@app.post("/quota/reset/{provider}")
async def quota_reset(provider: str):
    ok = usage_tracker.reset(provider)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Provider inconnu : {provider}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Settings — API keys
# ---------------------------------------------------------------------------

_API_KEY_NAMES = ["GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY"]


class ApiKeysRequest(BaseModel):
    GEMINI_API_KEY: Optional[str] = None
    GROQ_API_KEY: Optional[str] = None
    CEREBRAS_API_KEY: Optional[str] = None
    MISTRAL_API_KEY: Optional[str] = None
    NVIDIA_API_KEY: Optional[str] = None


@app.get("/settings/api-keys")
async def api_keys_get():
    return {k: bool(os.environ.get(k, "").strip()) for k in _API_KEY_NAMES}


@app.put("/settings/api-keys")
async def api_keys_put(req: ApiKeysRequest):
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _ENV_FILE.exists():
        _ENV_FILE.write_text("", encoding="utf-8")
    updated = False
    for k in _API_KEY_NAMES:
        val = getattr(req, k, None)
        if val is not None:
            dotenv_set_key(str(_ENV_FILE), k, val)
            updated = True
    if updated:
        load_dotenv(str(_ENV_FILE), override=True)
        llm.reload_dotenv()
        models_registry.invalidate()
    return {"ok": True}


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
# Memory
# ---------------------------------------------------------------------------

@app.get("/memory/profile")
async def memory_profile_get():
    return memory.load_profile()


@app.put("/memory/profile")
async def memory_profile_put(request: Request):
    data = await request.json()
    memory.save_profile(data)
    return {"ok": True}


@app.get("/memory/sessions")
async def memory_sessions_get():
    return {"sessions": memory.get_all_sessions()}


class ArchiveRequest(BaseModel):
    dates: list[str]


@app.post("/memory/sessions/archive")
async def memory_sessions_archive(req: ArchiveRequest):
    memory.archive_sessions(req.dates)
    return {"ok": True}


class AddSessionRequest(BaseModel):
    matiere: str
    fichier: str = ""
    erreurs: list = []
    reussies: int = 0
    ratees: int = 0


@app.post("/memory/sessions")
async def memory_session_add(req: AddSessionRequest):
    memory.add_session(req.matiere, req.fichier, req.erreurs, req.reussies, req.ratees)
    return {"ok": True}


@app.get("/memory/context")
async def memory_context_get():
    loop = asyncio.get_running_loop()
    profile = await loop.run_in_executor(None, memory.load_profile)
    sessions = await loop.run_in_executor(None, memory.get_all_sessions)
    forces = profile.get("forces", [])
    lacunes = profile.get("lacunes_confirmées", [])
    style = profile.get("préférences_interaction", {}).get("style", "")
    consol_log = await loop.run_in_executor(None, consolidation_engine.get_log, 1)
    last_consol = consol_log[0]["date"][:10] if consol_log else "jamais"
    lines = ["📊 Profil apprenant :"]
    lines.append(f"Forces : {', '.join(forces[:5])}" if forces else "Forces : (aucune enregistrée)")
    lines.append(f"Lacunes confirmées : {', '.join(lacunes[:5])}" if lacunes else "Lacunes : (aucune confirmée)")
    if style:
        lines.append(f"Style : {style}")
    lines.append(f"Dernière consolidation : {last_consol}")
    lines.append(f"Sessions totales : {len(sessions)}")
    return {"context": "\n".join(lines)}


@app.get("/memory/lacunes")
async def memory_lacunes_get():
    loop = asyncio.get_running_loop()
    profile = await loop.run_in_executor(None, memory.load_profile)
    sessions = await loop.run_in_executor(None, memory.get_sessions, 7)
    lacunes = profile.get("lacunes_confirmées", [])
    errors: list[dict] = []
    for s in sessions[-20:]:
        for e in s.get("erreurs", []):
            errors.append({"date": s.get("date", ""), "erreur": e})
    return {"lacunes": lacunes, "erreurs_recentes": errors}


# ---------------------------------------------------------------------------
# Context / Settings
# ---------------------------------------------------------------------------

@app.get("/context")
async def context_get():
    return memory.get_context()


@app.patch("/context/settings")
async def context_settings(request: Request):
    body = await request.json()
    allowed = {"modèle_actif", "strict_mode", "session_instruction", "consolidation_cloud", "orchestrateur_actif"}
    filtered = {k: v for k, v in body.items() if k in allowed}
    memory.update_context(**filtered)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

_SUPPORTED_EXT = {'.pdf', '.docx', '.txt', '.md', '.csv', '.json', '.png', '.jpg', '.jpeg', '.webp'}


async def _stream_load_sse(paths: list[str]):
    """Async generator: index files, stream summary tokens as SSE, send done event."""
    loop = asyncio.get_running_loop()
    total_pages = 0
    text_parts: list[str] = []
    indexed_paths: list[str] = []

    for path in paths:
        ext = Path(path).suffix.lower()
        if ext not in _SUPPORTED_EXT:
            logger.warning("Extension non supportée : %s", path)
            continue
        if not os.path.exists(path):
            logger.warning("Fichier non trouvé : %s", path)
            continue
        try:
            await loop.run_in_executor(None, rag.index_file, path)
            text = await loop.run_in_executor(None, RAGEngine.read_file_text, path)
            text_parts.append(text[:3000])
            if ext == '.pdf':
                reader = pypdf.PdfReader(path)
                total_pages += len(reader.pages)
            indexed_paths.append(path)
        except Exception:
            logger.exception("Erreur chargement fichier %s", path)

    memory.update_context(fichiers_actifs=indexed_paths, résumé_contexte="")

    accumulated = ""
    if text_parts:
        combined = "\n\n---\n\n".join(text_parts)[:12000]
        prompt = (
            "Résume en 100-150 mots maximum ces documents de cours. "
            "Indique les sujets principaux et les notions clés. Sois factuel.\n\n"
            f"Contenu :\n{combined}"
        )
        ctx = memory.get_context()
        model_override = ctx.get("modèle_actif") or None
        queue: asyncio.Queue = asyncio.Queue()

        def _worker(msgs, q, lp, model):
            try:
                for token in llm.stream(msgs, model=model):
                    asyncio.run_coroutine_threadsafe(q.put(token), lp)
            except Exception as exc:
                logger.exception("Erreur streaming résumé fichiers")
                asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
            finally:
                asyncio.run_coroutine_threadsafe(q.put(None), lp)

        Thread(
            target=_worker,
            args=([{"role": "user", "content": prompt}], queue, loop, model_override),
            daemon=True,
        ).start()

        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, dict) and "error" in item:
                break
            accumulated += item
            yield f"data: {json.dumps({'type': 'token', 'content': item}, ensure_ascii=False)}\n\n"

    memory.update_context(résumé_contexte=accumulated)

    chunks_count = 0
    if indexed_paths:
        try:
            result = rag._col.get(
                where={"source": {"$in": indexed_paths}}, include=[]
            )
            chunks_count = len(result.get("ids", []))
        except Exception:
            logger.exception("Erreur comptage chunks")

    yield f"data: {json.dumps({'type': 'done', 'pages': total_pages, 'chunks': chunks_count})}\n\n"


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class LoadFilesRequest(BaseModel):
    paths: list[str]


@app.post("/files/load")
async def files_load(req: LoadFilesRequest):
    return StreamingResponse(
        _stream_load_sse(req.paths), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@app.post("/files/upload")
async def files_upload(files: list[UploadFile] = File(...)):
    _fiches_dir = fiches_root()
    _fiches_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    for upload in files:
        filename = upload.filename or "upload.bin"
        ext = Path(filename).suffix.lower()
        if ext not in _SUPPORTED_EXT:
            continue
        dest = _fiches_dir / filename
        content = await upload.read()
        dest.write_bytes(content)
        saved_paths.append(str(dest))
    if not saved_paths:
        raise HTTPException(
            status_code=400,
            detail="Types supportés : PDF, DOCX, TXT, MD, CSV, JSON, PNG, JPG, JPEG, WEBP",
        )
    return StreamingResponse(
        _stream_load_sse(saved_paths), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@app.get("/files/active")
async def files_active():
    return memory.get_context()


@app.delete("/files/active")
async def files_active_delete():
    memory.update_context(fichiers_actifs=[], résumé_contexte="")
    return {"ok": True}




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


@app.get("/rag/files")
async def rag_files():
    loop = asyncio.get_running_loop()
    files = await loop.run_in_executor(None, rag.get_indexed_files)
    return {"files": files}




# ---------------------------------------------------------------------------
# Orchestrator presets
# ---------------------------------------------------------------------------

class PresetCreateRequest(BaseModel):
    nom: str
    effort: str
    steps: list[dict]


@app.get("/orchestrator/presets")
async def orchestrator_presets_list():
    loop = asyncio.get_running_loop()
    presets = await loop.run_in_executor(None, orchestrator.get_presets)
    return {"presets": presets}


@app.post("/orchestrator/presets")
async def orchestrator_presets_create(req: PresetCreateRequest):
    loop = asyncio.get_running_loop()
    preset = await loop.run_in_executor(None, orchestrator.create_preset, req.nom, req.effort, req.steps)
    return preset


@app.delete("/orchestrator/presets/{preset_id}")
async def orchestrator_presets_delete(preset_id: str):
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, orchestrator.delete_preset, preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Preset introuvable ou preset par défaut")
    return {"ok": True}


@app.post("/memory/consolidate")
async def memory_consolidate(request: Request):
    body = await request.json()
    use_cloud = bool(body.get("use_cloud", False))
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, consolidation_engine.consolidate_all, use_cloud)
    return result


@app.get("/memory/consolidation-log")
async def memory_consolidation_log():
    loop = asyncio.get_running_loop()
    log = await loop.run_in_executor(None, consolidation_engine.get_log)
    return {"log": log}








# ── Montage des routeurs de modules ─────────────────────────────────────────
# Monte tous les modules actifs disposant d'un modules/<id>/router.py (core ou
# non). Les modules core pas encore migrés restent décorés sur `app` ci-dessus.
_register_routers(app)
