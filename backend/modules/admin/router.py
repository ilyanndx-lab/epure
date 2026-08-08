"""Routeur du module Admin (organisation des fiches). Prefix /admin.

admin_engine injecté depuis core.runtime.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from threading import Thread

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.paths import user_data_roots
from core.runtime import SSE_HEADERS, admin_engine

logger = logging.getLogger(__name__)

router = APIRouter()


def _openable_roots() -> list[Path]:
    """Dossiers dont on accepte d'ouvrir un fichier dans l'explorateur.

    Délègue à ``core.paths.user_data_roots()`` : une seule définition des
    dossiers qu'un client peut désigner, partagée avec /files/load et
    /docanalysis/load. La liste locale d'origine oubliait les dossiers
    surveillés (``fiches.watch_folders``), souvent hors de la racine des fiches —
    une fiche parfaitement légitime y renvoyait un 403.

    Recalculés à chaque appel : la racine des fiches vient de la config
    d'instance et peut changer sans redémarrage.
    """
    return user_data_roots()


class ExecuteActionsRequest(BaseModel):
    actions: list[dict]


class UndoRequest(BaseModel):
    action_id: str


async def _stream_admin_scan():
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _worker():
        try:
            for result, index, total in admin_engine.scan_all():
                asyncio.run_coroutine_threadsafe(
                    queue.put({"result": result, "index": index, "total": total}), loop
                )
        except Exception as exc:
            logger.exception("Erreur scan_all admin")
            asyncio.run_coroutine_threadsafe(queue.put({"error": str(exc)}), loop)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    Thread(target=_worker, daemon=True).start()

    results = []
    while True:
        item = await queue.get()
        if item is None:
            break
        if "error" in item:
            yield f"data: {json.dumps({'type': 'error', 'content': item['error']}, ensure_ascii=False)}\n\n"
            return
        r = item["result"]
        results.append(r)
        yield f"data: {json.dumps({'type': 'progress', 'file': r['nom_actuel'], 'index': item['index'], 'total': item['total']}, ensure_ascii=False)}\n\n"

    yield f"data: {json.dumps({'type': 'done', 'résultats': results}, ensure_ascii=False)}\n\n"


@router.post("/scan")
async def admin_scan():
    return StreamingResponse(
        _stream_admin_scan(), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/duplicates")
async def admin_duplicates():
    loop = asyncio.get_running_loop()
    try:
        groups = await loop.run_in_executor(None, admin_engine.find_duplicates)
        return {"groupes": groups}
    except Exception:
        logger.exception("Erreur détection doublons")
        raise HTTPException(status_code=500, detail="Erreur détection doublons")


@router.post("/execute")
async def admin_execute(req: ExecuteActionsRequest):
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(None, admin_engine.execute_actions, req.actions)
        return {"résultats": results}
    except Exception:
        logger.exception("Erreur exécution actions admin")
        raise HTTPException(status_code=500, detail="Erreur exécution actions")


@router.get("/open")
async def admin_open(path: str):
    """Révèle un fichier dans l'explorateur de fichiers du système.

    Écrivait auparavant ``explorer /select,"{path}"`` dans un shell : le chemin
    venant du client, ``?path=x" & calc.exe & "`` fermait la chaîne et
    enchaînait une commande arbitraire. Plus de shell, et le chemin doit
    désigner un fichier réel sous l'un des dossiers de travail d'Épure —
    l'endpoint sert à ouvrir une fiche, pas n'importe quoi sur le disque.
    """
    target = Path(path).expanduser()
    try:
        target = target.resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="Chemin invalide")

    # Confinement AVANT le test d'existence : sinon la réponse permet de sonder
    # la présence de fichiers hors périmètre. resolve() + is_relative_to() et
    # jamais un startswith de chaînes (cf. codeagent._safe_path, CLAUDE.md §3.5).
    if not any(target.is_relative_to(r.resolve()) for r in _openable_roots()):
        raise HTTPException(status_code=403, detail="Chemin hors des dossiers autorisés")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Fichier introuvable")

    try:
        if os.name == "nt":
            # ATTENTION : explorer exige « /select,<chemin> » en UN SEUL
            # argument — le découper en ["explorer", "/select,", chemin] ouvre
            # le dossier Documents au lieu de sélectionner le fichier.
            subprocess.Popen(["explorer", f"/select,{target}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
        return {"ok": True}
    except Exception:
        logger.exception("Erreur ouverture %s", target)
        raise HTTPException(status_code=500, detail="Erreur ouverture fichier")


@router.get("/log")
async def admin_log():
    loop = asyncio.get_running_loop()
    log = await loop.run_in_executor(None, admin_engine.get_log)
    return {"log": log}


@router.post("/undo")
async def admin_undo(req: UndoRequest):
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, admin_engine.undo_action, req.action_id)
    return result
