import asyncio
import json
import logging
import re
from threading import Thread
from typing import Optional

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.llm import LLMEngine
from core.rag import RAGEngine

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

for _folder in _cfg.get("rag", {}).get("watch_folders", []):
    rag.watch(_folder)

_KHOLLE_SYSTEM = (
    "Tu es un professeur de kholle de classe préparatoire scientifique (MPSI/MP). "
    "Tu poses une question à la fois, tu écoutes la réponse de l'élève, tu la corriges "
    "avec rigueur en pointant les erreurs exactes et les imprécisions, tu donnes la réponse "
    "attendue si nécessaire, puis tu passes à la question suivante. Sois exigeant mais "
    "pédagogue. Ne pose jamais deux questions en même temps."
)


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
            history.append(json.loads(data))
            queue: asyncio.Queue = asyncio.Queue()

            user_message = history[-1].get("content", "")
            chunks = await loop.run_in_executor(None, rag.query, user_message)

            messages = list(history)
            if chunks:
                system_msg = {
                    "role": "system",
                    "content": (
                        "Contexte extrait de tes fiches de révision :\n"
                        f"{chunks}\n\n"
                        "Réponds à la question suivante en te basant sur ce contexte si pertinent."
                    ),
                }
                messages = [system_msg] + messages

            def _stream(msgs: list[dict], q: asyncio.Queue, lp: asyncio.AbstractEventLoop):
                try:
                    for token in llm.stream(msgs):
                        asyncio.run_coroutine_threadsafe(q.put(token), lp)
                except Exception as exc:
                    logger.exception("Erreur streaming chat")
                    asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(None), lp)

            Thread(target=_stream, args=(messages, queue, loop), daemon=True).start()

            accumulated = ""
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, dict) and "error" in item:
                    await websocket.send_text(json.dumps({"type": "error", "content": item["error"]}))
                    break
                accumulated += item
                await websocket.send_text(json.dumps({"type": "token", "content": item}))

            history.append({"role": "assistant", "content": accumulated})
            await websocket.send_text(json.dumps({"type": "done"}))

    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

@app.get("/rag/files")
async def rag_files():
    loop = asyncio.get_running_loop()
    files = await loop.run_in_executor(None, rag.get_indexed_files)
    return {"files": files}


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
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="source_files requis pour le mode generate")
        questions = await loop.run_in_executor(None, _generate_questions, req.source_files)
    elif req.mode == "list":
        if not req.questions:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="questions requises pour le mode list")
        questions = [q.strip() for q in req.questions if q.strip()]
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="mode invalide, valeurs acceptées : generate, list")
    return {"questions": questions}


@app.websocket("/ws/kholle")
async def ws_kholle(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_running_loop()

    questions: list = []
    current_index = 0
    session_errors: list = []

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")

            if msg_type == "start":
                questions = msg["questions"]
                current_index = 0
                session_errors = []
                await websocket.send_text(json.dumps({
                    "type": "question",
                    "content": questions[0],
                    "index": 0,
                    "total": len(questions),
                }))

            elif msg_type == "answer":
                answer = msg["content"]
                question = questions[current_index]

                correction_msgs = [
                    {"role": "system", "content": _KHOLLE_SYSTEM},
                    {
                        "role": "user",
                        "content": f"Question posée : {question}\nRéponse de l'élève : {answer}",
                    },
                ]

                queue: asyncio.Queue = asyncio.Queue()

                def _stream_correction(msgs, q, lp):
                    try:
                        for token in llm.stream(msgs):
                            asyncio.run_coroutine_threadsafe(q.put(token), lp)
                    except Exception as exc:
                        logger.exception("Erreur streaming correction kholle")
                        asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
                    finally:
                        asyncio.run_coroutine_threadsafe(q.put(None), lp)

                Thread(
                    target=_stream_correction,
                    args=(correction_msgs, queue, loop),
                    daemon=True,
                ).start()

                accumulated = ""
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, dict) and "error" in item:
                        await websocket.send_text(json.dumps({"type": "error", "content": item["error"]}))
                        break
                    accumulated += item
                    await websocket.send_text(json.dumps({"type": "token", "content": item}))

                # Extract errors without blocking the WS loop
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
                    await websocket.send_text(json.dumps({"type": "session_end", "errors": flat}))
                else:
                    await websocket.send_text(json.dumps({
                        "type": "question",
                        "content": questions[current_index],
                        "index": current_index,
                        "total": len(questions),
                    }))

    except WebSocketDisconnect:
        pass

