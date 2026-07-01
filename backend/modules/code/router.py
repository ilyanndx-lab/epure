"""Routeur du module Code (agent de code). Monté avec prefix "" (chemins
complets) car il expose REST /code/* ET le WebSocket /ws/code.

Helpers d'exécution importés de core.codeagent ; moteurs partagés (code_agent,
llm, quota_tracker, provider_of, pick_reflection_model) injectés via core.runtime.
"""

import asyncio
import json
import logging
from pathlib import Path as _P
from threading import Thread

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.auth import ws_require_token
from core.codeagent import (
    execute_code as _code_exec,
    create_file as _code_create,
    read_file as _code_read,
    delete_path as _code_delete,
    create_folder as _code_mkdir,
    rename_path as _code_rename,
    get_tree as _code_tree,
    SecurityError as _CodeSecurityError,
    install_package as _code_install,
    generate_tests as _code_generate_tests,
)
from core.runtime import (
    SSE_HEADERS,
    code_agent,
    llm,
    pick_reflection_model as _pick_reflection_model,
    provider_of as _provider_of,
    quota_tracker,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class CodeFileRequest(BaseModel):
    path: str
    content: str = ""


class CodeFolderRequest(BaseModel):
    path: str


class CodeRenameRequest(BaseModel):
    old: str
    new: str


@router.get("/code/files")
async def code_files():
    loop = asyncio.get_running_loop()
    tree = await loop.run_in_executor(None, _code_tree)
    return {"tree": tree}


@router.get("/code/file")
async def code_file_get(path: str):
    loop = asyncio.get_running_loop()
    try:
        content = await loop.run_in_executor(None, _code_read, path)
    except _CodeSecurityError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("Erreur lecture fichier code %s", path)
        raise HTTPException(status_code=500, detail="Erreur lecture")
    return {"content": content, "path": path}


@router.post("/code/file")
async def code_file_post(req: CodeFileRequest):
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _code_create, req.path, req.content)
    except _CodeSecurityError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("Erreur création fichier code %s", req.path)
        raise HTTPException(status_code=500, detail="Erreur création")
    return {"ok": True, "result": result}


@router.delete("/code/file")
async def code_file_delete(path: str):
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _code_delete, path)
    except _CodeSecurityError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True, "result": result}


@router.post("/code/folder")
async def code_folder_create(req: CodeFolderRequest):
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _code_mkdir, req.path)
    except _CodeSecurityError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True, "result": result}


@router.post("/code/rename")
async def code_rename(req: CodeRenameRequest):
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _code_rename, req.old, req.new)
    except _CodeSecurityError as e:
        raise HTTPException(status_code=403, detail=str(e))
    ok = not result.startswith("Erreur")
    return {"ok": ok, "result": result}


class CodeInstallRequest(BaseModel):
    package: str


async def _stream_pip_install(package: str):
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _worker():
        try:
            for event in _code_install(package):
                asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        except Exception as exc:
            logger.exception("Erreur install_package %s", package)
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "error", "line": str(exc)}), loop
            )
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    Thread(target=_worker, daemon=True).start()

    while True:
        event = await queue.get()
        if event is None:
            break
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/code/install")
async def code_install(req: CodeInstallRequest):
    return StreamingResponse(
        _stream_pip_install(req.package),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/code/usage")
async def code_usage():
    return quota_tracker.get_usage()


@router.post("/code/usage/reset")
async def code_usage_reset():
    quota_tracker.reset()
    return {"ok": True}


@router.post("/code/execute")
async def code_execute_direct(req: CodeFileRequest):
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _code_exec, req.path, req.content)
    except _CodeSecurityError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return result


@router.websocket("/ws/code")
async def ws_code(websocket: WebSocket):
    if not await ws_require_token(websocket):
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")

            if msg_type == "message":
                content = msg.get("content", "")
                file_context = msg.get("file_context", "")
                pipeline = msg.get("pipeline") or None
                history = msg.get("history") or None

                # Pipeline présent → utiliser ses modèles ; sinon fallback legacy
                if pipeline:
                    model = (pipeline.get("code") or {}).get("model") or None
                    reflection_model = (pipeline.get("reflection") or {}).get("model") or None
                else:
                    model = msg.get("model") or None
                    reflection_model = _pick_reflection_model(model)

                queue: asyncio.Queue = asyncio.Queue()

                def _agent_worker(q, _content, _file_ctx, _model, _ref_model, _pipeline, _history):
                    try:
                        for event in code_agent.run_turn(
                            _content, _file_ctx, model=_model,
                            reflection_model=_ref_model, pipeline=_pipeline,
                            history=_history,
                        ):
                            asyncio.run_coroutine_threadsafe(q.put(event), loop)
                    except Exception as exc:
                        logger.exception("Erreur CodeAgent.run_turn")
                        asyncio.run_coroutine_threadsafe(
                            q.put({"type": "error", "content": str(exc)}), loop
                        )
                    finally:
                        asyncio.run_coroutine_threadsafe(q.put(None), loop)

                Thread(
                    target=_agent_worker,
                    args=(queue, content, file_context, model, reflection_model, pipeline, history),
                    daemon=True,
                ).start()

                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    # Intercepter les events tokens pour le quota tracker
                    if event.get("type") == "tokens":
                        step = event.get("step", "other")
                        if pipeline:
                            step_model = (pipeline.get(step) or {}).get("model") or model
                        else:
                            step_model = reflection_model if step == "reflection" else model
                        provider = _provider_of(step_model)
                        quota_tracker.track(step, provider, event.get("count", 0))
                    await websocket.send_text(json.dumps(event, ensure_ascii=False))

            elif msg_type == "generate_tests":
                path = msg.get("path", "")
                model = msg.get("model") or None
                queue2: asyncio.Queue = asyncio.Queue()

                def _tests_worker(q, _path, _model):
                    try:
                        test_content = ""
                        for token in _code_generate_tests(_path, llm, _model):
                            test_content += token
                            asyncio.run_coroutine_threadsafe(
                                q.put({"type": "tests_token", "content": token}), loop
                            )
                        # Compute test file path
                        stem = _P(_path).stem
                        test_path = str(_P(_path).parent / f"test_{stem}.py")
                        asyncio.run_coroutine_threadsafe(
                            q.put({"type": "tests_done", "path": test_path,
                                   "count": max(1, int(len(test_content.split()) * 1.3))}),
                            loop,
                        )
                    except Exception as exc:
                        logger.exception("Erreur generate_tests %s", path)
                        asyncio.run_coroutine_threadsafe(
                            q.put({"type": "error", "content": str(exc)}), loop
                        )
                    finally:
                        asyncio.run_coroutine_threadsafe(q.put(None), loop)

                Thread(target=_tests_worker, args=(queue2, path, model), daemon=True).start()

                while True:
                    event = await queue2.get()
                    if event is None:
                        break
                    if event.get("type") == "tests_done":
                        quota_tracker.track("tests", _provider_of(model), event.get("count", 0))
                    await websocket.send_text(json.dumps(event, ensure_ascii=False))

            elif msg_type == "execute_confirm":
                path = msg.get("path", "")
                args = msg.get("args", "")
                try:
                    result = await loop.run_in_executor(None, _code_exec, path, args)
                except _CodeSecurityError as e:
                    result = {"stdout": "", "stderr": str(e), "returncode": -1, "duration_ms": 0}
                if result.get("html_preview"):
                    await websocket.send_text(json.dumps(
                        {"type": "html_preview", "content": result.get("content", "")},
                        ensure_ascii=False,
                    ))
                elif result.get("external"):
                    await websocket.send_text(json.dumps(
                        {"type": "execute_external", "path": path},
                        ensure_ascii=False,
                    ))
                else:
                    send = {k: v for k, v in result.items() if k not in ("html_preview", "external", "content")}
                    await websocket.send_text(json.dumps(
                        {"type": "execute_result", **send}, ensure_ascii=False
                    ))

    except WebSocketDisconnect:
        pass
