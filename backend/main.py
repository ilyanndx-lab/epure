import asyncio
import io
import json
import logging
import os
import re
import shutil
from pathlib import Path
from threading import Thread
from typing import Optional

import ollama
import pypdf
import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.flashcards import FlashcardsEngine
from core.llm import LLMEngine
from core.memory import MemoryEngine
from core.rag import RAGEngine
from core.voice import PiperEngine, WhisperEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

with open("config.yaml") as f:
    _cfg = yaml.safe_load(f)

llm = LLMEngine()
rag = RAGEngine()
memory = MemoryEngine()  # resets context_session on startup
flashcards_engine = FlashcardsEngine()

_voice_cfg = _cfg.get("voice", {})
whisper = WhisperEngine(
    model_size=_voice_cfg.get("whisper_model", "small"),
    language=_voice_cfg.get("language", "fr"),
)
piper = PiperEngine(
    voice=_voice_cfg.get("piper_voice", "fr_FR-upmc-medium"),
)

for _folder in _cfg.get("rag", {}).get("watch_folders", []):
    rag.watch(_folder)

_FICHES_DIR = Path(r"C:\Users\Ilyan\Fiches")

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
    try:
        response = await loop.run_in_executor(None, ollama.list)
        models = [m.model for m in response.models]
    except Exception:
        logger.exception("Erreur liste modèles Ollama")
        models = [llm._model]
    return {"models": models}


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
    ctx_str = await loop.run_in_executor(None, memory.build_system_context)
    return {"context": ctx_str if ctx_str else "(mémoire vide)"}


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
    allowed = {"modèle_actif", "strict_mode", "session_instruction"}
    filtered = {k: v for k, v in body.items() if k in allowed}
    memory.update_context(**filtered)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

async def _stream_load_sse(paths: list[str]):
    """Async generator: index PDFs, stream summary tokens as SSE, send done event."""
    loop = asyncio.get_running_loop()
    total_pages = 0
    text_parts: list[str] = []
    indexed_paths: list[str] = []

    for path in paths:
        if not os.path.exists(path):
            logger.warning("Fichier non trouvé : %s", path)
            continue
        try:
            await loop.run_in_executor(None, rag.index_pdf, path)
            text = await loop.run_in_executor(None, RAGEngine.read_pdf_text, path)
            text_parts.append(text[:3000])
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
    _FICHES_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    for upload in files:
        dest = _FICHES_DIR / (upload.filename or "upload.pdf")
        content = await upload.read()
        dest.write_bytes(content)
        saved_paths.append(str(dest))
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
            text = await loop.run_in_executor(None, RAGEngine.read_pdf_text, path)
            text_parts.append(text[:3000])
        except Exception:
            logger.exception("Erreur lecture PDF %s pour /résumé", path)

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
    try:
        response = await loop.run_in_executor(None, ollama.list)
        models = [m.model for m in response.models]
        ctx = memory.get_context()
        active_model = ctx.get("modèle_actif", llm._model)
        return {"ollama": True, "model": active_model, "models": models}
    except Exception:
        logger.exception("Erreur health check Ollama")
        return {"ollama": False, "model": "", "models": []}


@app.get("/rag/files")
async def rag_files():
    loop = asyncio.get_running_loop()
    files = await loop.run_in_executor(None, rag.get_indexed_files)
    return {"files": files}


# ---------------------------------------------------------------------------
# Flashcards
# ---------------------------------------------------------------------------

def _parse_cartes_json(raw: str, max_n: int = 50) -> list:
    cleaned = re.sub(r'```(?:json)?\s*|\s*```', '', raw).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and "cartes" in data:
            cartes = data["cartes"][:max_n]
            if cartes and "question" in cartes[0]:
                return cartes
    except (json.JSONDecodeError, IndexError):
        pass
    match = re.search(r'"cartes"\s*:\s*(\[.*?\])\s*[,}]', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))[:max_n]
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Impossible de parser les flashcards : {raw[:300]}")


class GenerateFlashcardsRequest(BaseModel):
    source: str
    nom: str
    n_cartes: Optional[int] = None


class ReviewRequest(BaseModel):
    resultat: str  # "su" | "pas_su"


async def _stream_flashcards_generate(req: GenerateFlashcardsRequest):
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(None, RAGEngine.read_pdf_text, req.source)
    except Exception:
        logger.exception("Erreur lecture PDF flashcards %s", req.source)
        yield f"data: {json.dumps({'type': 'error', 'content': 'Impossible de lire le PDF'})}\n\n"
        return

    n_instructions = (
        f"Génère exactement {req.n_cartes} flashcards."
        if req.n_cartes
        else "Choisis toi-même le nombre optimal (entre 10 et 50) selon la densité du document."
    )
    prompt = (
        "Tu es un professeur de classe préparatoire scientifique (MPSI/MP).\n"
        "À partir du contenu de cours suivant, crée des flashcards de révision.\n"
        f"{n_instructions}\n"
        "Chaque flashcard : question précise (définition, théorème, propriété, démo-clé) "
        "et réponse concise et rigoureuse.\n\n"
        f"Contenu :\n{text[:14000]}\n\n"
        "Réponds UNIQUEMENT avec ce JSON valide, sans texte avant ou après :\n"
        '{"cartes": [{"question": "...", "réponse": "..."}, ...]}'
    )

    ctx = memory.get_context()
    model_override = ctx.get("modèle_actif") or None
    queue: asyncio.Queue = asyncio.Queue()

    def _worker(msgs, q, lp, model):
        try:
            result = llm.generate(msgs, model=model)
            asyncio.run_coroutine_threadsafe(q.put(result), lp)
        except Exception as exc:
            logger.exception("Erreur génération flashcards")
            asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)

    Thread(
        target=_worker,
        args=([{"role": "user", "content": prompt}], queue, loop, model_override),
        daemon=True,
    ).start()

    # Yield a "." every 2 s while waiting for the LLM to finish
    accumulated = None
    while accumulated is None:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=2.0)
            if isinstance(item, dict) and "error" in item:
                yield f"data: {json.dumps({'type': 'error', 'content': item['error']})}\n\n"
                return
            accumulated = item
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'token', 'content': '.'})}\n\n"

    try:
        cartes = _parse_cartes_json(accumulated, req.n_cartes or 50)
    except ValueError as exc:
        logger.error("Parse flashcards JSON : %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    deck_id = flashcards_engine.create_deck(req.nom, req.source, cartes)
    yield f"data: {json.dumps({'type': 'done', 'deck_id': deck_id, 'n_cartes': len(cartes)})}\n\n"


@app.get("/flashcards/decks")
async def flashcards_decks_list():
    loop = asyncio.get_running_loop()
    decks = await loop.run_in_executor(None, flashcards_engine.get_decks)
    return {"decks": decks}


@app.get("/flashcards/decks/{deck_id}")
async def flashcards_deck_get(deck_id: str):
    loop = asyncio.get_running_loop()
    deck = await loop.run_in_executor(None, flashcards_engine.get_deck, deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="Deck introuvable")
    return deck


@app.delete("/flashcards/decks/{deck_id}")
async def flashcards_deck_delete(deck_id: str):
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, flashcards_engine.delete_deck, deck_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Deck introuvable")
    return {"ok": True}


@app.post("/flashcards/generate")
async def flashcards_generate(req: GenerateFlashcardsRequest):
    return StreamingResponse(
        _stream_flashcards_generate(req),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@app.post("/flashcards/decks/{deck_id}/cartes/{carte_id}/review")
async def flashcards_review(deck_id: str, carte_id: str, req: ReviewRequest):
    if req.resultat not in ("su", "pas_su"):
        raise HTTPException(status_code=400, detail="resultat doit être 'su' ou 'pas_su'")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, flashcards_engine.update_carte, deck_id, carte_id, req.resultat
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Carte introuvable")
    return result


@app.get("/flashcards/due")
async def flashcards_due():
    loop = asyncio.get_running_loop()
    due = await loop.run_in_executor(None, flashcards_engine.get_due)
    return {"cartes": due}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    history: list[dict] = []
    loop = asyncio.get_running_loop()

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            history.append({"role": msg["role"], "content": msg["content"]})

            rag_override: str | None = msg.get("rag_override")
            strict_override: bool = bool(msg.get("strict_override", False))

            ctx = memory.get_context()
            active_files = ctx.get("fichiers_actifs", [])
            model_override = ctx.get("modèle_actif") or None

            user_text = msg["content"]
            if rag_override == "all":
                chunks = await loop.run_in_executor(None, rag.query, user_text)
            elif active_files:
                chunks = await loop.run_in_executor(
                    None, rag.query_filtered, user_text, active_files
                )
            else:
                chunks = ""

            sys_parts: list[str] = []
            if strict_override:
                sys_parts.append(
                    "Réponds de façon maximalement concise. "
                    "Pas d'introduction, pas de reformulation."
                )
            mem_ctx = memory.build_system_context()
            if mem_ctx:
                sys_parts.append(mem_ctx)
            if chunks:
                sys_parts.append(
                    "Contexte extrait de tes fiches de révision :\n"
                    f"{chunks}\n\n"
                    "Réponds à la question en te basant sur ce contexte si pertinent."
                )

            messages = list(history)
            if sys_parts:
                messages = [{"role": "system", "content": "\n\n".join(sys_parts)}] + messages

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
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, dict) and "error" in item:
                    await websocket.send_text(
                        json.dumps({"type": "error", "content": item["error"]})
                    )
                    break
                accumulated += item
                await websocket.send_text(json.dumps({"type": "token", "content": item}))

            history.append({"role": "assistant", "content": accumulated})
            await websocket.send_text(json.dumps({"type": "done"}))

    except WebSocketDisconnect:
        pass


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
                mem_ctx = memory.build_system_context()

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
