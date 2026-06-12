"""Routeur du module Historique. Monté sous le prefix /history.

Moteur partagé injecté depuis core.runtime (pas de duplication de logique).
"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.runtime import history_engine

router = APIRouter()


class HistorySearchRequest(BaseModel):
    query: str


@router.get("")
async def history_list():
    loop = asyncio.get_running_loop()
    conversations = await loop.run_in_executor(None, history_engine.list_conversations)
    return conversations


@router.get("/{conv_id}")
async def history_get(conv_id: str):
    loop = asyncio.get_running_loop()
    conv = await loop.run_in_executor(None, history_engine.get_conversation, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return conv


@router.delete("/{conv_id}")
async def history_delete(conv_id: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, history_engine.delete_conversation, conv_id)
    return {"ok": True}


@router.post("/search")
async def history_search(req: HistorySearchRequest):
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, history_engine.search_history, req.query)
    return {"results": results}
