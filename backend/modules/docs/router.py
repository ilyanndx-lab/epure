"""Routeur du module Docs (analyse documentaire). Monté avec prefix "" : il
expose REST /docanalysis/* ET le WebSocket /ws/docchat.

Moteurs partagés (docanalysis, llm, memory) injectés via core.runtime.
"""

import asyncio
import json
import logging
from pathlib import Path
from threading import Thread
from typing import Optional

from fastapi import APIRouter, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.auth import ws_require_token
from core.runtime import SSE_HEADERS, docanalysis, llm, memory

logger = logging.getLogger(__name__)

router = APIRouter()

_DOC_UPLOADS = Path(__file__).parent.parent.parent / "doc_uploads"


class DocLoadPathRequest(BaseModel):
    path: str


class DocSearchRequest(BaseModel):
    doc_id: str
    query: str
    n_results: int = 5


class DocDeepenRequest(BaseModel):
    chunks: list[str]
    query: Optional[str] = None
    use_cloud: bool = False


class DocSummarizeRequest(BaseModel):
    doc_id: str
    level: str = "short"
    use_cloud: bool = False


async def _stream_docload(path: str):
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _worker():
        try:
            for event in docanalysis.load_document_streaming(path):
                asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        except Exception as exc:
            logger.exception("Erreur load_document_streaming %s", path)
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "error", "message": str(exc)}), loop
            )
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    Thread(target=_worker, daemon=True).start()

    while True:
        event = await queue.get()
        if event is None:
            break
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/docanalysis/load")
async def docanalysis_load(req: DocLoadPathRequest):
    return StreamingResponse(
        _stream_docload(req.path), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post("/docanalysis/upload")
async def docanalysis_upload(file: UploadFile = File(...)):
    _DOC_UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = _DOC_UPLOADS / (file.filename or "upload.pdf")
    content = await file.read()
    dest.write_bytes(content)
    return StreamingResponse(
        _stream_docload(str(dest)), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/docanalysis/docs")
async def docanalysis_docs_list():
    loop = asyncio.get_running_loop()
    docs = await loop.run_in_executor(None, docanalysis.get_loaded_docs)
    return {"docs": docs}


@router.delete("/docanalysis/docs/{doc_id}")
async def docanalysis_unload(doc_id: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, docanalysis.unload_document, doc_id)
    return {"ok": True}


async def _stream_tokens_from_generator(gen_fn, *args, **kwargs):
    """Runs a synchronous generator in a thread and yields SSE token events."""
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _worker():
        try:
            for token in gen_fn(*args, **kwargs):
                asyncio.run_coroutine_threadsafe(queue.put(token), loop)
        except Exception as exc:
            logger.exception("Erreur streaming docanalysis")
            asyncio.run_coroutine_threadsafe(
                queue.put({"error": str(exc)}), loop
            )
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    Thread(target=_worker, daemon=True).start()

    while True:
        item = await queue.get()
        if item is None:
            break
        if isinstance(item, dict):
            continue  # skip stats or error dicts
        yield f"data: {json.dumps({'type': 'token', 'content': item}, ensure_ascii=False)}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


async def _stream_docsearch(doc_id: str, query: str, n_results: int):
    loop = asyncio.get_running_loop()

    results = await loop.run_in_executor(None, docanalysis.search, doc_id, query, n_results)
    yield f"data: {json.dumps({'type': 'chunks', 'results': results}, ensure_ascii=False)}\n\n"

    if not results:
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    chunks_text = [r["chunk"] for r in results]
    async for line in _stream_tokens_from_generator(docanalysis.summarize_section, chunks_text, query):
        yield line


@router.post("/docanalysis/search")
async def docanalysis_search(req: DocSearchRequest):
    return StreamingResponse(
        _stream_docsearch(req.doc_id, req.query, req.n_results),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/docanalysis/deepen")
async def docanalysis_deepen(req: DocDeepenRequest):
    ctx = memory.get_context()
    model = ctx.get("modèle_actif") if req.use_cloud else None
    return StreamingResponse(
        _stream_tokens_from_generator(docanalysis.summarize_section, req.chunks, req.query, model),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/docanalysis/summarize")
async def docanalysis_summarize(req: DocSummarizeRequest):
    ctx = memory.get_context()
    model = ctx.get("modèle_actif") if req.use_cloud else None
    return StreamingResponse(
        _stream_tokens_from_generator(docanalysis.summarize_document, req.doc_id, req.level, model),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.websocket("/ws/docchat")
async def ws_docchat(websocket: WebSocket):
    if not await ws_require_token(websocket):
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()
    history: list[dict] = []

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            doc_id = msg.get("doc_id", "")
            content = msg.get("content", "")
            if not content:
                continue

            history.append({"role": "user", "content": content})

            doc_info = await loop.run_in_executor(None, docanalysis.get_doc_info, doc_id)
            titre = doc_info["titre"] if doc_info else "document"

            chunks_list = await loop.run_in_executor(
                None, docanalysis.search, doc_id, content, 5
            )
            ctx_text = "\n\n---\n\n".join(r["chunk"] for r in chunks_list)

            system_content = (
                f"Tu es un assistant spécialisé dans l'analyse de ce document : {titre}. "
                "Réponds uniquement à partir du contenu du document."
            )
            if ctx_text:
                system_content += f"\n\nExtraits pertinents :\n{ctx_text}"

            messages = [{"role": "system", "content": system_content}] + list(history)

            queue: asyncio.Queue = asyncio.Queue()

            def _stream_docchat(msgs, q, lp):
                try:
                    for token in llm.stream(msgs):
                        if isinstance(token, dict) and token.get("__stats__"):
                            dur = token.get("eval_duration_ns", 0)
                            out_tok = token.get("output_tokens", 0)
                            tok_s = round(out_tok / (dur / 1e9), 1) if dur > 0 else 0.0
                            asyncio.run_coroutine_threadsafe(
                                q.put({"__stats__": True, "tok_s": tok_s}), lp
                            )
                        elif isinstance(token, str):
                            asyncio.run_coroutine_threadsafe(q.put(token), lp)
                except Exception as exc:
                    logger.exception("Erreur streaming docchat")
                    asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(None), lp)

            Thread(target=_stream_docchat, args=(messages, queue, loop), daemon=True).start()

            assistant_text = ""
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, dict):
                    if item.get("__stats__"):
                        await websocket.send_text(
                            json.dumps({"type": "stats", "tok_s": item.get("tok_s", 0)})
                        )
                    elif "error" in item:
                        await websocket.send_text(
                            json.dumps({"type": "error", "content": item["error"]})
                        )
                    continue
                assistant_text += item
                await websocket.send_text(json.dumps({"type": "token", "content": item}))

            history.append({"role": "assistant", "content": assistant_text})
            await websocket.send_text(json.dumps({"type": "done"}))

    except WebSocketDisconnect:
        pass
