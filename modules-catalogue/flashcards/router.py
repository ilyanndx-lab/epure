"""Routeur du module Flashcards. Monté sous le prefix /flashcards.

Moteurs partagés (llm, memory, flashcards_engine) injectés depuis core.runtime.
"""

import asyncio
import json
import logging
import re
from threading import Thread
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.rag import RAGEngine
from core.runtime import SSE_HEADERS, flashcards_engine, llm, memory

logger = logging.getLogger(__name__)

router = APIRouter()


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


@router.get("/decks")
async def flashcards_decks_list():
    loop = asyncio.get_running_loop()
    decks = await loop.run_in_executor(None, flashcards_engine.get_decks)
    return {"decks": decks}


@router.get("/decks/{deck_id}")
async def flashcards_deck_get(deck_id: str):
    loop = asyncio.get_running_loop()
    deck = await loop.run_in_executor(None, flashcards_engine.get_deck, deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="Deck introuvable")
    return deck


@router.delete("/decks/{deck_id}")
async def flashcards_deck_delete(deck_id: str):
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, flashcards_engine.delete_deck, deck_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Deck introuvable")
    return {"ok": True}


@router.post("/generate")
async def flashcards_generate(req: GenerateFlashcardsRequest):
    return StreamingResponse(
        _stream_flashcards_generate(req),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/decks/{deck_id}/cartes/{carte_id}/review")
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


@router.get("/due")
async def flashcards_due():
    loop = asyncio.get_running_loop()
    due = await loop.run_in_executor(None, flashcards_engine.get_due)
    return {"cartes": due}
