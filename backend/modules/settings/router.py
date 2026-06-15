"""Routeur du module Réglages / plateforme. Monté avec prefix "" : il regroupe
les endpoints transverses qui sous-tendent l'UI Réglages et les services
partagés (voix, fichiers, mémoire, contexte, quotas, clés API, presets
orchestrateur). Les chemins sont conservés tels quels (aucun changement d'API).

Moteurs partagés injectés via core.runtime.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from typing import Optional

import pypdf
from dotenv import load_dotenv, set_key as dotenv_set_key
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.instance import fiches_root, instance_config
from core.rag import RAGEngine
from core.runtime import (
    API_KEY_NAMES,
    SSE_HEADERS,
    consolidation_engine,
    llm,
    memory,
    models_registry,
    orchestrator,
    piper,
    rag,
    usage_tracker,
    whisper,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"
_SUPPORTED_EXT = {'.pdf', '.docx', '.txt', '.md', '.csv', '.json', '.png', '.jpg', '.jpeg', '.webp'}


# ── Voice ────────────────────────────────────────────────────────────────────

class SynthesizeRequest(BaseModel):
    text: str
    voice: str = "fr_FR-upmc-medium"


@router.post("/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(None, whisper.transcribe, audio_bytes)
    except Exception:
        logger.exception("Erreur transcription /voice/transcribe")
        raise HTTPException(status_code=500, detail="Erreur transcription")
    return {"text": text}


@router.post("/voice/synthesize")
async def voice_synthesize(req: SynthesizeRequest):
    loop = asyncio.get_running_loop()
    try:
        wav_bytes = await loop.run_in_executor(None, piper.synthesize, req.text)
    except Exception:
        logger.exception("Erreur synthèse /voice/synthesize")
        raise HTTPException(status_code=500, detail="Erreur synthèse vocale")
    return StreamingResponse(io.BytesIO(wav_bytes), media_type="audio/wav")


# ── Quota / Usage ────────────────────────────────────────────────────────────

@router.get("/quota/usage")
async def quota_usage():
    return usage_tracker.get_usage()


@router.post("/quota/reset/{provider}")
async def quota_reset(provider: str):
    ok = usage_tracker.reset(provider)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Provider inconnu : {provider}")
    return {"ok": True}


@router.get("/quota/deepseek-balance")
def deepseek_balance():
    """Crédit DeepSeek en temps réel via l'API officielle (GET /user/balance).

    DeepSeek est une API payante : on suit le solde restant (pas des tokens/req).
    Réponse : {ok, is_available, balances:[{currency,total_balance,...}], raison?}.
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return {"ok": False, "raison": "DEEPSEEK_API_KEY non configurée dans les Réglages."}
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) epure/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "ok": True,
            "is_available": bool(data.get("is_available", False)),
            "balances": data.get("balance_infos", []),
        }
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "raison": f"Clé DeepSeek refusée (HTTP {exc.code})."}
        return {"ok": False, "raison": f"Erreur DeepSeek (HTTP {exc.code})."}
    except Exception as exc:
        logger.warning("DeepSeek balance: %s", exc)
        return {"ok": False, "raison": f"Solde DeepSeek indisponible : {exc}"}


# ── Settings — API keys ──────────────────────────────────────────────────────

class ApiKeysRequest(BaseModel):
    GEMINI_API_KEY: Optional[str] = None
    GROQ_API_KEY: Optional[str] = None
    CEREBRAS_API_KEY: Optional[str] = None
    MISTRAL_API_KEY: Optional[str] = None
    NVIDIA_API_KEY: Optional[str] = None
    DEEPSEEK_API_KEY: Optional[str] = None


@router.get("/settings/api-keys")
async def api_keys_get():
    return {k: bool(os.environ.get(k, "").strip()) for k in API_KEY_NAMES}


@router.put("/settings/api-keys")
async def api_keys_put(req: ApiKeysRequest):
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _ENV_FILE.exists():
        _ENV_FILE.write_text("", encoding="utf-8")
    updated = False
    for k in API_KEY_NAMES:
        val = getattr(req, k, None)
        if val is not None:
            dotenv_set_key(str(_ENV_FILE), k, val)
            updated = True
    if updated:
        load_dotenv(str(_ENV_FILE), override=True)
        llm.reload_dotenv()
        models_registry.invalidate()
    return {"ok": True}


# ── Memory ───────────────────────────────────────────────────────────────────

@router.get("/memory/profile")
async def memory_profile_get():
    return memory.load_profile()


@router.put("/memory/profile")
async def memory_profile_put(request: Request):
    data = await request.json()
    memory.save_profile(data)
    return {"ok": True}


@router.get("/memory/sessions")
async def memory_sessions_get():
    return {"sessions": memory.get_all_sessions()}


class ArchiveRequest(BaseModel):
    dates: list[str]


@router.post("/memory/sessions/archive")
async def memory_sessions_archive(req: ArchiveRequest):
    memory.archive_sessions(req.dates)
    return {"ok": True}


class AddSessionRequest(BaseModel):
    matiere: str
    fichier: str = ""
    erreurs: list = []
    reussies: int = 0
    ratees: int = 0


@router.post("/memory/sessions")
async def memory_session_add(req: AddSessionRequest):
    memory.add_session(req.matiere, req.fichier, req.erreurs, req.reussies, req.ratees)
    return {"ok": True}


@router.get("/memory/context")
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


@router.get("/memory/lacunes")
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


# ── Context / Settings ───────────────────────────────────────────────────────

@router.get("/context")
async def context_get():
    return memory.get_context()


@router.patch("/context/settings")
async def context_settings(request: Request):
    body = await request.json()
    allowed = {"modèle_actif", "strict_mode", "session_instruction", "consolidation_cloud", "orchestrateur_actif"}
    filtered = {k: v for k, v in body.items() if k in allowed}
    memory.update_context(**filtered)
    return {"ok": True}


# ── Files ────────────────────────────────────────────────────────────────────

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
                    # Résumé fichiers : texte uniquement. Écarte les sentinelles
                    # dict (__stats__, __reasoning__) — sinon `accumulated += item`
                    # plus bas lèverait un TypeError.
                    if not isinstance(token, str):
                        continue
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


class LoadFilesRequest(BaseModel):
    paths: list[str]


@router.post("/files/load")
async def files_load(req: LoadFilesRequest):
    return StreamingResponse(
        _stream_load_sse(req.paths), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post("/files/upload")
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
        _stream_load_sse(saved_paths), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/files/active")
async def files_active():
    return memory.get_context()


@router.delete("/files/active")
async def files_active_delete():
    memory.update_context(fichiers_actifs=[], résumé_contexte="")
    return {"ok": True}


# ── RAG ──────────────────────────────────────────────────────────────────────

@router.get("/rag/files")
async def rag_files():
    loop = asyncio.get_running_loop()
    files = await loop.run_in_executor(None, rag.get_indexed_files)
    return {"files": files}


# ── Orchestrator presets ─────────────────────────────────────────────────────

class PresetCreateRequest(BaseModel):
    nom: str
    effort: str
    steps: list[dict]


@router.get("/orchestrator/presets")
async def orchestrator_presets_list():
    loop = asyncio.get_running_loop()
    presets = await loop.run_in_executor(None, orchestrator.get_presets)
    return {"presets": presets}


@router.post("/orchestrator/presets")
async def orchestrator_presets_create(req: PresetCreateRequest):
    loop = asyncio.get_running_loop()
    preset = await loop.run_in_executor(None, orchestrator.create_preset, req.nom, req.effort, req.steps)
    return preset


@router.delete("/orchestrator/presets/{preset_id}")
async def orchestrator_presets_delete(preset_id: str):
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, orchestrator.delete_preset, preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Preset introuvable ou preset par défaut")
    return {"ok": True}


@router.post("/memory/consolidate")
async def memory_consolidate(request: Request):
    body = await request.json()
    use_cloud = bool(body.get("use_cloud", False))
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, consolidation_engine.consolidate_all, use_cloud)
    return result


@router.get("/memory/consolidation-log")
async def memory_consolidation_log():
    loop = asyncio.get_running_loop()
    log = await loop.run_in_executor(None, consolidation_engine.get_log)
    return {"log": log}


# ── Atelier : tests de connectivité des moteurs ───────────────────────────────

@router.post("/settings/test/aider")
def test_aider():
    """Teste que le binaire `aider` (atelier.aider_path) répond à --version."""
    atelier = instance_config.get().get("atelier") or {}
    ap = (atelier.get("aider_path") or "aider").strip()
    bin_path = shutil.which(ap) or shutil.which(ap + ".cmd") or (ap if os.path.exists(ap) else None)
    if not bin_path:
        return {"ok": False, "version": "", "raison": f"Binaire '{ap}' introuvable dans le PATH."}
    try:
        r = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0
        version = (r.stdout or r.stderr or "").strip().splitlines()[0] if ok else ""
        return {"ok": ok, "version": version, "raison": "" if ok else r.stderr.strip()[:200]}
    except Exception as exc:
        return {"ok": False, "version": "", "raison": str(exc)}


@router.post("/settings/test/gateway")
def test_gateway():
    """Teste que la passerelle claude_gateway est joignable."""
    from core.module_workshop import gateway_reachable, _gateway_cfg

    gw = _gateway_cfg()
    url = gw["base_url"]
    ok = gateway_reachable(url)
    return {"ok": ok, "url": url, "raison": "" if ok else f"Passerelle injoignable : {url}"}


@router.post("/settings/gateway/start")
def gateway_start():
    """Démarre la passerelle via atelier.gateway.start_command (process détaché)."""
    from core.module_workshop import start_gateway

    return start_gateway()


# Providers cloud → clé API qui les active (réutilise les listes statiques de
# core.models comme source des modèles, plutôt que de les redéfinir).
_PROVIDER_KEY = {
    "nvidia": "NVIDIA_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# Fallback Gemini si core.models ne l'expose pas (ne devrait pas arriver).
_GEMINI_FALLBACK = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro"]


@router.get("/settings/provider-models")
def provider_models():
    """Modèles disponibles par provider cloud, limités aux providers dont la clé
    API est définie dans l'environnement. Les IDs suivent la convention Épure
    `provider:model_id` (ex. "gemini:gemini-2.0-flash")."""
    from core import models as _models
    from core.models import (
        _NVIDIA_STATIC, _GROQ_STATIC, _CEREBRAS_STATIC, _MISTRAL_STATIC, _DEEPSEEK_STATIC,
    )

    provider_static = {
        "nvidia": _NVIDIA_STATIC,
        "groq": _GROQ_STATIC,
        "cerebras": _CEREBRAS_STATIC,
        "mistral": _MISTRAL_STATIC,
        "gemini": getattr(_models, "_GEMINI_STATIC", None) or _GEMINI_FALLBACK,
        "deepseek": _DEEPSEEK_STATIC,
    }
    result: dict[str, list[str]] = {}
    for provider, models in provider_static.items():
        key_name = _PROVIDER_KEY.get(provider, "")
        if os.environ.get(key_name, "").strip():
            result[provider] = [f"{provider}:{mid}" for mid in models]
    return {"providers": result}
