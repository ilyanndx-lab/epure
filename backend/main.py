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

_KHOLLE_SYSTEM = (
    "Tu es un professeur de kholle de classe préparatoire scientifique (MPSI/MP). "
    "Tu poses une question à la fois, tu écoutes la réponse de l'élève, tu la corriges "
    "avec rigueur en pointant les erreurs exactes et les imprécisions, tu donnes la réponse "
    "attendue si nécessaire, puis tu passes à la question suivante. Sois exigeant mais "
    "pédagogue. Ne pose jamais deux questions en même temps."
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
# Skills endpoints
# ---------------------------------------------------------------------------

async def _stream_résumé_sse():
    ctx = memory.get_context()
    active_files = ctx.get("fichiers_actifs", [])
    if not active_files:
        yield (
            f"data: {json.dumps({'type': 'error', 'content': 'Aucun fichier actif. Chargez des fichiers via le panneau 📎.'})}\n\n"
        )
        return

    loop = asyncio.get_running_loop()
    text_parts: list[str] = []
    for path in active_files:
        try:
            text = await loop.run_in_executor(None, RAGEngine.read_file_text, path)
            text_parts.append(text[:3000])
        except Exception:
            logger.exception("Erreur lecture fichier %s pour /résumé", path)

    if not text_parts:
        yield f"data: {json.dumps({'type': 'error', 'content': 'Impossible de lire les fichiers actifs.'})}\n\n"
        return

    combined = "\n\n---\n\n".join(text_parts)[:12000]
    prompt = (
        "Résume en 100-150 mots maximum ces documents de cours. "
        "Indique les sujets principaux et les notions clés. Sois factuel.\n\n"
        f"Contenu :\n{combined}"
    )
    model_override = ctx.get("modèle_actif") or None
    queue: asyncio.Queue = asyncio.Queue()

    def _worker(msgs, q, lp, model):
        try:
            for token in llm.stream(msgs, model=model):
                asyncio.run_coroutine_threadsafe(q.put(token), lp)
        except Exception as exc:
            logger.exception("Erreur streaming /skills/résumé")
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
            yield f"data: {json.dumps({'type': 'error', 'content': item['error']})}\n\n"
            return
        yield f"data: {json.dumps({'type': 'token', 'content': item}, ensure_ascii=False)}\n\n"


@app.post("/skills/résumé")
async def skills_résumé():
    return StreamingResponse(
        _stream_résumé_sse(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


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
# Chat
# ---------------------------------------------------------------------------

_WEB_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_WEB_SEARCH_TIMEOUT = 8.0
# User-Agent alternatifs essayés en cas de blocage (403 Cloudflare, etc.)
_WEB_SEARCH_USER_AGENTS = [
    _WEB_SEARCH_UA,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]

# Cache mémoire LRU avec TTL court : évite de re-frapper DuckDuckGo pour une
# même requête (utile quand l'utilisateur reformule peu ou relance @web).
_WEB_SEARCH_CACHE_TTL = 300.0  # secondes
_WEB_SEARCH_CACHE_MAX = 64
_web_search_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()


def _web_search_cache_get(key: str) -> Optional[str]:
    """Retourne la valeur en cache si présente et non expirée, sinon None."""
    entry = _web_search_cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if (time.time() - ts) > _WEB_SEARCH_CACHE_TTL:
        _web_search_cache.pop(key, None)
        return None
    _web_search_cache.move_to_end(key)  # marque comme récemment utilisé
    return value


def _web_search_cache_set(key: str, value: str) -> None:
    """Insère/rafraîchit une entrée et évince les plus anciennes (LRU)."""
    _web_search_cache[key] = (time.time(), value)
    _web_search_cache.move_to_end(key)
    while len(_web_search_cache) > _WEB_SEARCH_CACHE_MAX:
        _web_search_cache.popitem(last=False)


def _web_search_fetch(url: str, accept: str) -> tuple[Optional[str], Optional[str]]:
    """Récupère une URL en essayant plusieurs User-Agent.

    Retourne ``(texte, None)`` en cas de succès, ``(None, erreur)`` sinon.
    """
    last_exc: Optional[str] = None
    for ua in _WEB_SEARCH_USER_AGENTS:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=_WEB_SEARCH_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning("Web search HTTP %s pour %s (UA: %s)", resp.status, url, ua)
                    last_exc = f"HTTP {resp.status}"
                    continue  # essayer prochain UA
                return resp.read().decode("utf-8", errors="replace"), None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            logger.warning("Web search impossible pour %s (UA: %s) : %s", url, ua, exc)
            last_exc = str(exc)
            continue
        except Exception as exc:  # pragma: no cover - imprévisible
            logger.exception("Web search erreur inattendue pour %s (UA: %s)", url, ua)
            last_exc = str(exc)
            continue
    return None, (last_exc or "erreur inconnue")


def _web_search_instant(q: str) -> tuple[list[str], list[str], Optional[str]]:
    """Stratégie 1 : API DuckDuckGo Instant Answer (JSON).

    Retourne ``(parties, lignes_source, erreur)``. ``parties`` est vide quand
    l'API ne renvoie rien d'exploitable (cas qui déclenche le fallback HTML).
    """
    params = {
        "q": q,
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
        "t": "epure",
    }
    url = "https://api.duckduckgo.com/" + urllib.parse.urlencode(params)
    raw, err = _web_search_fetch(url, "application/json")
    if raw is None:
        return [], [], err

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Web search JSON invalide pour %s", q)
        return [], [], "réponse JSON invalide"

    abstract = (data.get("Abstract") or "").strip()
    abstract_source = (data.get("AbstractSource") or "").strip()
    abstract_url = (data.get("AbstractURL") or "").strip()
    definition = (data.get("Definition") or "").strip()
    definition_source = (data.get("DefinitionSource") or "").strip()
    definition_url = (data.get("DefinitionURL") or "").strip()
    answer = (data.get("Answer") or "").strip()
    answer_type = (data.get("AnswerType") or "").strip()

    related: list[str] = []
    for item in data.get("RelatedTopics", []) or []:
        if not isinstance(item, dict):
            continue
        # Les RelatedTopics imbriqués (sous "Topics") sont groupés par sujet
        if "Topics" in item and isinstance(item["Topics"], list):
            for sub in item["Topics"]:
                text = (sub.get("Text") or "").strip()
                if text:
                    related.append(text)
        else:
            text = (item.get("Text") or "").strip()
            if text:
                related.append(text)

    parts: list[str] = []
    if abstract:
        parts.append(abstract)
    if definition and definition != abstract:
        parts.append(f"Définition : {definition}")
    if answer:
        prefix = f"Réponse ({answer_type})" if answer_type else "Réponse"
        parts.append(f"{prefix} : {answer}")
    for r in related[:5]:
        parts.append(f"- {r}")

    source_lines: list[str] = []
    if abstract and abstract_source:
        source_lines.append(f"Source abstract : {abstract_source}" + (f" ({abstract_url})" if abstract_url else ""))
    if definition and definition_source:
        source_lines.append(f"Source définition : {definition_source}" + (f" ({definition_url})" if definition_url else ""))
    return parts, source_lines, None


_HTML_RESULT_RE = re.compile(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_HTML_SNIPPET_RE = re.compile(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(fragment: str) -> str:
    """Retire les balises et déséchappe les entités d'un fragment HTML."""
    return _htmllib.unescape(_HTML_TAG_RE.sub("", fragment)).strip()


def _web_search_html(q: str) -> tuple[list[str], Optional[str]]:
    """Stratégie 2 (fallback) : endpoint HTML html.duckduckgo.com.

    Retourne ``(parties, erreur)``. Parse les résultats (titre + extrait) par
    expression régulière — pas de dépendance HTML externe.
    """
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
    raw, err = _web_search_fetch(url, "text/html")
    if raw is None:
        return [], err

    titles = [_strip_html(m) for m in _HTML_RESULT_RE.findall(raw)]
    snippets = [_strip_html(m) for m in _HTML_SNIPPET_RE.findall(raw)]

    parts: list[str] = []
    for i in range(max(len(titles), len(snippets))):
        title = titles[i] if i < len(titles) else ""
        snippet = snippets[i] if i < len(snippets) else ""
        line = " — ".join(p for p in (title, snippet) if p)
        if line:
            parts.append(f"- {line}")
        if len(parts) >= 5:
            break
    return parts, None


def perform_web_search(query: str) -> str:
    """Recherche web via DuckDuckGo, avec fallback HTML et cache LRU.

    Stratégie : (1) API Instant Answer (JSON) ; (2) si rien d'exploitable,
    fallback sur l'endpoint HTML html.duckduckgo.com. Le résultat formaté est
    mis en cache (TTL court). En cas d'échec réseau total, retourne un message
    d'erreur court pour informer l'utilisateur.
    """
    if not query or not query.strip():
        return ""
    q = query.strip()

    cached = _web_search_cache_get(q)
    if cached is not None:
        logger.info("Web search « %s » : %d caractères servis depuis le cache", q, len(cached))
        return cached

    # Stratégie 1 : Instant Answer
    parts, source_lines, err = _web_search_instant(q)
    source = "DuckDuckGo Instant Answer"

    # Stratégie 2 : fallback HTML si l'Instant Answer ne donne rien
    if not parts:
        html_parts, html_err = _web_search_html(q)
        if html_parts:
            parts = html_parts
            source_lines = []
            source = "DuckDuckGo HTML"
        elif err and html_err:
            # Les deux stratégies ont échoué au niveau réseau
            logger.error("Web search échoué pour « %s » : instant=%s ; html=%s", q, err, html_err)
            return f"Erreur de recherche web : {err}"

    if not parts:
        # Aucun résultat exploitable, mais pas d'erreur réseau
        logger.info("Web search « %s » : 0 résultat (source: %s)", q, source)
        return ""

    sources_block = ("\n\n" + "\n".join(source_lines)) if source_lines else ""
    result = (
        f"Résultats de recherche web pour « {q} » ({source}) :\n"
        + "\n".join(parts)
        + sources_block
    )

    excerpt = result[:160].replace("\n", " ")
    logger.info("Web search « %s » : %d résultat(s) via %s — extrait : %s", q, len(parts), source, excerpt)

    _web_search_cache_set(q, result)
    return result


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    history: list[dict] = []
    loop = asyncio.get_running_loop()
    _last_model: list[str] = [llm._model]

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            history.append({"role": msg["role"], "content": msg["content"]})

            rag_override: str | None = msg.get("rag_override")
            strict_override: bool = bool(msg.get("strict_override", False))
            web_search_override: bool = bool(msg.get("web_search_override", False))

            ctx = memory.get_context()
            active_files = ctx.get("fichiers_actifs", [])
            model_override = ctx.get("modèle_actif") or None
            _last_model[0] = model_override or llm._model

            _req_start = time.time()
            user_text = msg["content"]

            # @historique skill
            hist_ctx = ""
            if "@historique" in user_text:
                hist_query = user_text.replace("@historique", "").strip() or user_text
                hist_results = await loop.run_in_executor(
                    None, history_engine.search_history, hist_query
                )
                if hist_results:
                    extraits = "\n\n".join(
                        f"— {r['titre']} ({r['date']}) :\n{r['extrait']}"
                        for r in hist_results
                    )
                    hist_ctx = f"Extraits de conversations précédentes pertinentes :\n{extraits}"
                user_text = hist_query if hist_query != user_text else user_text.replace("@historique", "").strip()
                history[-1]["content"] = user_text or msg["content"]

            # @web skill
            web_ctx = ""
            if web_search_override:
                _t_web = time.time()
                web_query = user_text.strip()
                web_results = await loop.run_in_executor(None, perform_web_search, web_query)
                logger.info("TTFT Web: %.3fs (query=%r, len=%d)", time.time() - _t_web, web_query[:80], len(web_results))
                if web_results:
                    web_ctx = (
                        "Résultats de recherche web récents (peuvent compléter tes connaissances) :\n"
                        f"{web_results}\n\n"
                        "Si pertinent, intègre ces informations dans ta réponse et cite la source."
                    )
                else:
                    web_ctx = (
                        "Recherche web : aucun résultat exploitable trouvé pour cette requête. "
                        "Réponds à partir de tes connaissances en le signalant."
                    )

            _t = time.time()
            if rag_override == "all":
                chunks = await loop.run_in_executor(None, rag.query, user_text)
            elif active_files:
                chunks = await loop.run_in_executor(
                    None, rag.query_filtered, user_text, active_files
                )
            else:
                chunks = ""
            logger.info("TTFT RAG: %.3fs", time.time() - _t)

            sys_parts: list[str] = []
            if strict_override:
                sys_parts.append(
                    "Réponds de façon maximalement concise. "
                    "Pas d'introduction, pas de reformulation."
                )
            _t = time.time()
            mem_ctx = await loop.run_in_executor(None, memory.build_system_context, user_text)
            logger.info("TTFT Memory: %.3fs", time.time() - _t)
            if mem_ctx:
                sys_parts.append(mem_ctx)
            if hist_ctx:
                sys_parts.append(hist_ctx)
            if web_ctx:
                sys_parts.append(web_ctx)
            if chunks:
                sys_parts.append(
                    "Contexte extrait de tes fiches de révision :\n"
                    f"{chunks}\n\n"
                    "Réponds à la question en te basant sur ce contexte si pertinent."
                )

            messages = list(history)
            if sys_parts:
                messages = [{"role": "system", "content": "\n\n".join(sys_parts)}] + messages

            # ── Orchestrator ──────────────────────────────────────────────────
            _effort = msg.get("effort", "direct")
            _client_steps = msg.get("steps", [])  # [{"role": "...", "model": "..."}]
            _direct_mode = bool(msg.get("direct", False)) or _effort == "direct" or not _effort

            if not _direct_mode:
                _pipeline: list[dict] = []

                if _effort == "adaptive":
                    try:
                        _classification = await asyncio.wait_for(
                            loop.run_in_executor(None, orchestrator.classify_task, user_text, ctx),
                            timeout=3.0,
                        )
                    except Exception:
                        _classification = {"complexity": "simple"}
                    _complexity = _classification.get("complexity", "simple")
                    if _complexity == "simple":
                        _direct_mode = True
                    else:
                        _eff = "medium" if _complexity == "moderate" else "high"
                        _pipeline = orchestrator.build_steps(_eff, [], ctx)
                elif _effort in ("low", "medium", "high"):
                    _pipeline = orchestrator.build_steps(_effort, _client_steps, ctx)

                if not _direct_mode and _pipeline:
                    await websocket.send_text(json.dumps({
                        "type": "pipeline_info",
                        "effort": _effort,
                        "steps": [{"role": s["role"], "label": s.get("label", s["role"]), "model": s["model"]} for s in _pipeline],
                    }))
                    _final = ""
                    async for _event in orchestrator.run_pipeline(_pipeline, user_text, messages, loop):
                        if _event.get("type") == "pipeline_done":
                            _final = _event.get("final_output", "")
                        await websocket.send_text(json.dumps(_event))
                    if _final:
                        history.append({"role": "assistant", "content": _final})
                    await websocket.send_text(json.dumps({"type": "done"}))
                    continue
                elif not _direct_mode:
                    _direct_mode = True  # empty pipeline → fall through to direct
            # ─────────────────────────────────────────────────────────────────

            queue: asyncio.Queue = asyncio.Queue()

            def _stream(msgs, q, lp, model):
                try:
                    for token in llm.stream(msgs, model=model):
                        asyncio.run_coroutine_threadsafe(q.put(token), lp)
                except Exception as exc:
                    logger.exception("Erreur streaming chat")
                    asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(None), lp)

            Thread(
                target=_stream, args=(messages, queue, loop, model_override), daemon=True
            ).start()

            accumulated = ""
            _first_token = True
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, dict) and "error" in item:
                    await websocket.send_text(
                        json.dumps({"type": "error", "content": item["error"]})
                    )
                    break
                if isinstance(item, dict) and "__stats__" in item:
                    usage_tracker.track(
                        _provider_of(_last_model[0]),
                        item.get("prompt_tokens", 0),
                        item.get("output_tokens", 0),
                    )
                    await websocket.send_text(json.dumps({
                        "type": "stats",
                        "prompt_tokens": item.get("prompt_tokens", 0),
                        "output_tokens": item.get("output_tokens", 0),
                        "eval_duration_ms": (item.get("eval_duration_ns", 0) or 0) // 1_000_000,
                        "prompt_duration_ms": (item.get("prompt_duration_ns", 0) or 0) // 1_000_000,
                    }))
                    continue
                if _first_token:
                    logger.info("TTFT total: %.3fs", time.time() - _req_start)
                    _first_token = False
                accumulated += item
                await websocket.send_text(json.dumps({"type": "token", "content": item}))

            if accumulated:
                history.append({"role": "assistant", "content": accumulated})
            await websocket.send_text(json.dumps({"type": "done"}))

    except WebSocketDisconnect:
        if len(history) >= 3:
            model = _last_model[0]
            msgs = list(history)
            use_cloud = memory.get_context().get("consolidation_cloud", False)

            def _save_and_consolidate():
                conv_id = history_engine.save_conversation(msgs, model, ["chat"])
                if len(msgs) >= 10:
                    consolidation_engine.consolidate_history(conv_id, use_cloud)

            Thread(target=_save_and_consolidate, daemon=True).start()


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


# ---------------------------------------------------------------------------
# Kholle
# ---------------------------------------------------------------------------

class KholleStartRequest(BaseModel):
    mode: str
    source_files: Optional[list] = None
    questions: Optional[list] = None


def _generate_questions(source_files: list) -> list:
    parts = []
    for path in source_files:
        try:
            text = RAGEngine.read_pdf_text(path)
            parts.append(text[:6000])
        except Exception:
            logger.exception("Erreur lecture PDF %s", path)
    if not parts:
        raise ValueError("Aucun contenu extrait des fichiers sélectionnés")

    content = "\n\n---\n\n".join(parts)[:14000]
    prompt = (
        "Tu es un professeur de kholle de classe préparatoire scientifique (MPSI/MP).\n"
        "À partir du contenu de cours suivant, génère 10 questions de kholle adaptées au niveau prépa.\n"
        "Les questions doivent être précises, demander des définitions rigoureuses ou des démonstrations, "
        "et couvrir les notions importantes du cours.\n\n"
        f"Contenu :\n{content}\n\n"
        "Réponds UNIQUEMENT avec un JSON valide, sans texte avant ou après :\n"
        '{"questions": ["question1", "question2", ..., "question10"]}'
    )
    raw = llm.generate([{"role": "user", "content": prompt}])
    return _parse_questions_json(raw)


def _parse_questions_json(raw: str) -> list:
    try:
        return json.loads(raw)["questions"]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    match = re.search(r'\{.*?"questions"\s*:\s*(\[[^\]]*\])', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    lines = [l.strip().strip('"').rstrip('",') for l in raw.splitlines()]
    questions = [l for l in lines if len(l) > 15]
    if questions:
        return questions[:10]
    raise ValueError(f"Impossible de parser les questions générées : {raw[:200]}")


def _extract_errors(correction: str, question: str, answer: str) -> list:
    try:
        prompt = (
            f"Analyse cette correction de kholle.\n\n"
            f"Question : {question}\n"
            f"Réponse élève : {answer}\n"
            f"Correction : {correction}\n\n"
            "Liste uniquement les erreurs ou imprécisions de la réponse de l'élève.\n"
            'Réponds UNIQUEMENT avec ce JSON valide : {"errors": ["erreur1", "erreur2"]}\n'
            'Si aucune erreur : {"errors": []}'
        )
        raw = llm.generate([{"role": "user", "content": prompt}])
        match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group()).get("errors", [])
    except Exception:
        logger.exception("Erreur lors de l'extraction des erreurs kholle")
    return []


@app.post("/kholle/start")
async def kholle_start(req: KholleStartRequest):
    loop = asyncio.get_running_loop()
    if req.mode == "generate":
        if not req.source_files:
            raise HTTPException(status_code=400, detail="source_files requis pour le mode generate")
        questions = await loop.run_in_executor(None, _generate_questions, req.source_files)
    elif req.mode == "list":
        if not req.questions:
            raise HTTPException(status_code=400, detail="questions requises pour le mode list")
        questions = [q.strip() for q in req.questions if q.strip()]
    else:
        raise HTTPException(status_code=400, detail="mode invalide, valeurs acceptées : generate, list")
    return {"questions": questions}


@app.websocket("/ws/kholle")
async def ws_kholle(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_running_loop()

    questions: list = []
    current_index = 0
    session_errors: list = []
    answers: list = []

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")

            if msg_type == "start":
                questions = msg["questions"]
                current_index = 0
                session_errors = []
                answers = []
                await websocket.send_text(json.dumps({
                    "type": "question",
                    "content": questions[0],
                    "index": 0,
                    "total": len(questions),
                }))

            elif msg_type == "answer":
                answer = msg["content"]
                question = questions[current_index]
                answers.append(answer)

                ctx = memory.get_context()
                model_override = ctx.get("modèle_actif") or None
                mem_ctx = await loop.run_in_executor(None, memory.build_system_context, question)

                system_content = _KHOLLE_SYSTEM
                if mem_ctx:
                    system_content = mem_ctx + "\n\n" + system_content

                correction_msgs = [
                    {"role": "system", "content": system_content},
                    {
                        "role": "user",
                        "content": f"Question posée : {question}\nRéponse de l'élève : {answer}",
                    },
                ]

                queue: asyncio.Queue = asyncio.Queue()

                def _stream_correction(msgs, q, lp, model):
                    try:
                        for token in llm.stream(msgs, model=model):
                            asyncio.run_coroutine_threadsafe(q.put(token), lp)
                    except Exception as exc:
                        logger.exception("Erreur streaming correction kholle")
                        asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
                    finally:
                        asyncio.run_coroutine_threadsafe(q.put(None), lp)

                Thread(
                    target=_stream_correction,
                    args=(correction_msgs, queue, loop, model_override),
                    daemon=True,
                ).start()

                accumulated = ""
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, dict) and "error" in item:
                        await websocket.send_text(
                            json.dumps({"type": "error", "content": item["error"]})
                        )
                        break
                    if isinstance(item, dict) and "__stats__" in item:
                        usage_tracker.track(
                            _provider_of(model_override or llm._model),
                            item.get("prompt_tokens", 0),
                            item.get("output_tokens", 0),
                        )
                        await websocket.send_text(json.dumps({
                            "type": "stats",
                            "prompt_tokens": item.get("prompt_tokens", 0),
                            "output_tokens": item.get("output_tokens", 0),
                            "eval_duration_ms": (item.get("eval_duration_ns", 0) or 0) // 1_000_000,
                            "prompt_duration_ms": (item.get("prompt_duration_ns", 0) or 0) // 1_000_000,
                        }))
                        continue
                    accumulated += item
                    await websocket.send_text(json.dumps({"type": "token", "content": item}))

                errors = await loop.run_in_executor(
                    None, _extract_errors, accumulated, question, answer
                )
                if errors:
                    session_errors.append({"question": question, "errors": errors})

                await websocket.send_text(json.dumps({"type": "done"}))

            elif msg_type == "next":
                current_index += 1
                if current_index >= len(questions):
                    flat = []
                    for item in session_errors:
                        q_short = item["question"][:60].rstrip()
                        for err in item["errors"]:
                            flat.append(f"[{q_short}…] {err}")
                    await websocket.send_text(
                        json.dumps({"type": "session_end", "errors": flat})
                    )

                    # Persist session to memory
                    try:
                        ctx = memory.get_context()
                        active_files = ctx.get("fichiers_actifs", [])
                        fichier = active_files[0] if active_files else ""
                        all_errors = []
                        for item in session_errors:
                            all_errors.extend(item["errors"])
                        réussies = len(questions) - len(session_errors)
                        await loop.run_in_executor(
                            None,
                            memory.add_session,
                            "kholle",
                            fichier,
                            all_errors,
                            réussies,
                            len(session_errors),
                        )
                        await loop.run_in_executor(None, memory.promote_lacunes)
                        # Non-blocking consolidation after kholle session
                        consol_data = {
                            "matière": "kholle",
                            "erreurs": all_errors,
                            "réussies": réussies,
                            "ratées": len(session_errors),
                        }
                        _use_cloud = memory.get_context().get("consolidation_cloud", False)
                        Thread(
                            target=lambda: consolidation_engine.consolidate_session(consol_data, _use_cloud),
                            daemon=True,
                        ).start()
                    except Exception:
                        logger.exception("Erreur sauvegarde session kholle en mémoire")
                else:
                    await websocket.send_text(json.dumps({
                        "type": "question",
                        "content": questions[current_index],
                        "index": current_index,
                        "total": len(questions),
                    }))

    except WebSocketDisconnect:
        pass






# ── Montage des routeurs de modules ─────────────────────────────────────────
# Monte tous les modules actifs disposant d'un modules/<id>/router.py (core ou
# non). Les modules core pas encore migrés restent décorés sur `app` ci-dessus.
_register_routers(app)
